"""Independent, coalescing KLayout render and local-snap workers."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
import logging
from pathlib import Path
import threading
import time
from typing import Any, Protocol, TypeAlias

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QImage, QTransform

from .klayout_geometry import Segment2D, plan_snap_search, select_snap
from .klayout_types import (
    Box2D,
    KLayoutConfig,
    Point2D,
    RenderFailure,
    RenderFrame,
    RenderRequest,
    SnapRequest,
    SnapResponse,
    SnapFailure,
    SnapWorkBudget,
    StructureBoundsFailure,
    StructureBoundsRequest,
    StructureBoundsResult,
    SNAP_UNAVAILABLE_CANCELLED,
    SNAP_UNAVAILABLE_CANDIDATE_BUDGET,
    SNAP_UNAVAILABLE_SHAPE_BUDGET,
    SNAP_UNAVAILABLE_TIME_BUDGET,
    forward_rotate_point,
    inverse_rotate_box,
    inverse_rotate_point,
)
from .model import SnapResult


logger = logging.getLogger(__name__)


class _RenderBackend(Protocol):
    def ensure_config(self, config: KLayoutConfig) -> None: ...

    def render(self, request: RenderRequest) -> RenderFrame: ...

    def close(self) -> None: ...


class _SnapBackend(Protocol):
    def ensure_config(self, config: KLayoutConfig) -> None: ...

    def snap(
        self,
        request: SnapRequest,
        *,
        is_cancelled: Callable[[], bool],
    ) -> SnapResponse: ...

    def close(self) -> None: ...


RenderBackendFactory = Callable[[], _RenderBackend]
SnapBackendFactory = Callable[[], _SnapBackend]
_STOP_WORKER = object()
_CREATOR_THREAD_ERROR = "KLayout worker methods must be called from the creator thread."


@dataclass(frozen=True)
class _Publication:
    kind: str
    value: object
    config_generation: int | None


@dataclass(frozen=True)
class _SnapWork:
    request: SnapRequest
    cancellation_generation: int
    hover_cancellation_generation: int


@dataclass(frozen=True)
class _StructurePublication:
    kind: str
    value: object
    request_id: int | None
    generation: int | None


_RETIRED_STRUCTURE_BOUNDS_WORKERS: set[QObject] = set()


class KLayoutRenderWorker(QObject):
    """Render on one daemon thread; lifecycle calls belong to the creator thread."""

    loaded = Signal(object)
    frame_ready = Signal(object)
    failed = Signal(object)
    lifecycle_failed = Signal(str)
    finished = Signal()
    _publication_posted = Signal(object)
    _finished_posted = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        backend_factory: RenderBackendFactory | None = None,
    ) -> None:
        super().__init__(parent)
        self._creator_thread_id = threading.get_ident()
        self._backend_factory = backend_factory or _KLayoutRenderBackend
        self._condition = threading.Condition(threading.Lock())
        self._stop_requested = threading.Event()
        self._pending: RenderRequest | None = None
        self._latest_config: KLayoutConfig | None = None
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

    def submit(self, request: RenderRequest) -> None:
        """Queue the newest render request from the worker's creator thread."""
        self._require_creator_thread()
        with self._condition:
            if self._stopping or self._stop_requested.is_set():
                return
            self._pending = request
            self._latest_config = request.config
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run,
                    name="klayout-render",
                    daemon=True,
                )
                self._thread.start()
            self._condition.notify()

    def stop(self, timeout_s: float = 1.0) -> None:
        """Stop accepting work from the creator thread and join up to the deadline."""
        self._require_creator_thread()
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._condition:
            self._stop_requested.set()
            self._stopping = True
            self._pending = None
            thread = self._thread
            self._condition.notify_all()
            post_finished = thread is None
        if post_finished:
            self._post_finished()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

    def _run(self) -> None:
        backend: _RenderBackend | None = None
        try:
            backend = self._backend_factory()
            active_config: KLayoutConfig | None = None
            while True:
                request = self._take_pending()
                if request is _STOP_WORKER:
                    return
                try:
                    if active_config != request.config:
                        backend.ensure_config(request.config)
                        active_config = request.config
                        self._post_publication(
                            "loaded",
                            request.config,
                            request.config,
                        )
                    frame = backend.render(request)
                except Exception as exc:
                    self._post_publication(
                        "failed",
                        RenderFailure(
                            request_id=request.request_id,
                            config_generation=request.config.generation,
                            viewport_generation=request.viewport_generation,
                            purpose=request.purpose,
                            message=f"{type(exc).__name__}: {exc}",
                        ),
                        request.config,
                    )
                    continue
                self._post_publication(
                    "frame_ready",
                    frame,
                    request.config,
                )
        except Exception as exc:
            self._post_publication(
                "lifecycle_failed",
                f"{type(exc).__name__}: {exc}",
            )
        finally:
            if backend is not None:
                try:
                    backend.close()
                except Exception as exc:
                    self._post_publication(
                        "lifecycle_failed",
                        f"{type(exc).__name__}: {exc}",
                    )
            self._post_finished()

    def _take_pending(self) -> RenderRequest | object:
        with self._condition:
            while (
                self._pending is None
                and not self._stopping
                and not self._stop_requested.is_set()
            ):
                self._condition.wait()
            if self._stopping or self._stop_requested.is_set():
                return _STOP_WORKER
            request = self._pending
            self._pending = None
            return request

    def _require_creator_thread(self) -> None:
        if threading.get_ident() != self._creator_thread_id:
            raise RuntimeError(_CREATOR_THREAD_ERROR)

    def _post_publication(
        self,
        kind: str,
        value: object,
        config: KLayoutConfig | None = None,
    ) -> None:
        self._publication_posted.emit(
            _Publication(
                kind=kind,
                value=value,
                config_generation=None if config is None else config.generation,
            )
        )

    def _post_finished(self) -> None:
        with self._condition:
            if self._finished_publication_posted:
                return
            self._finished_publication_posted = True
            self._thread_finished_event.set()
        self._finished_posted.emit()

    @property
    def is_finished(self) -> bool:
        return self._thread_finished_event.is_set()

    @Slot(object)
    def _deliver_publication(self, publication: _Publication) -> None:
        with self._condition:
            if self._stopping or self._stop_requested.is_set():
                return
            if publication.config_generation is not None and (
                self._latest_config is None
                or publication.config_generation != self._latest_config.generation
            ):
                return
        getattr(self, publication.kind).emit(publication.value)

    @Slot()
    def _deliver_finished(self) -> None:
        self.finished.emit()


class KLayoutSnapWorker(QObject):
    """Snap on one daemon thread; lifecycle calls belong to the creator thread."""

    loaded = Signal(object)
    snap_ready = Signal(object)
    failed = Signal(object)
    lifecycle_failed = Signal(str)
    finished = Signal()
    _publication_posted = Signal(object)
    _finished_posted = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        backend_factory: SnapBackendFactory | None = None,
    ) -> None:
        super().__init__(parent)
        self._creator_thread_id = threading.get_ident()
        self._backend_factory = backend_factory or _KLayoutSnapBackend
        self._condition = threading.Condition(threading.Lock())
        self._stop_requested = threading.Event()
        self._hover: SnapRequest | None = None
        self._clicks: deque[SnapRequest] = deque()
        self._cancellation_generation = 0
        self._hover_cancellation_generation = 0
        self._latest_config: KLayoutConfig | None = None
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

    def submit_hover(self, request: SnapRequest) -> None:
        """Queue the newest hover request from the worker's creator thread."""
        self._require_creator_thread()
        self._submit(request, click=False)

    def submit_click(self, request: SnapRequest) -> None:
        """Queue a FIFO-priority click request from the worker's creator thread."""
        self._require_creator_thread()
        self._submit(request, click=True)

    def cancel_hover(self) -> None:
        self._require_creator_thread()
        with self._condition:
            self._hover_cancellation_generation += 1
            self._hover = None
            self._condition.notify_all()

    def cancel_pending(self) -> None:
        self._require_creator_thread()
        with self._condition:
            self._cancellation_generation += 1
            self._hover_cancellation_generation += 1
            self._hover = None
            self._clicks.clear()
            self._condition.notify_all()

    def stop(self, timeout_s: float = 1.0) -> None:
        """Stop accepting work from the creator thread and join up to the deadline."""
        self._require_creator_thread()
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._condition:
            self._stop_requested.set()
            self._stopping = True
            self._hover = None
            self._clicks.clear()
            thread = self._thread
            self._condition.notify_all()
            post_finished = thread is None
        if post_finished:
            self._post_finished()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

    def _submit(self, request: SnapRequest, *, click: bool) -> None:
        with self._condition:
            if self._stopping or self._stop_requested.is_set():
                return
            self._latest_config = request.config
            if click:
                self._clicks.append(request)
            else:
                self._hover = request
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run,
                    name="klayout-snap",
                    daemon=True,
                )
                self._thread.start()
            self._condition.notify()

    def _run(self) -> None:
        backend: _SnapBackend | None = None
        try:
            backend = self._backend_factory()
            active_config: KLayoutConfig | None = None
            while True:
                work = self._take_next()
                if work is _STOP_WORKER:
                    return
                request = work.request
                try:
                    config_changed = active_config != request.config
                    if config_changed:
                        backend.ensure_config(request.config)
                        active_config = request.config
                    if self._work_is_obsolete(work):
                        continue
                    if config_changed:
                        self._post_publication(
                            "loaded",
                            request.config,
                            request.config,
                        )
                    response = backend.snap(
                        request,
                        is_cancelled=lambda work=work: self._work_is_obsolete(work),
                    )
                    if self._work_is_obsolete(work):
                        continue
                except Exception as exc:
                    if self._work_is_obsolete(work):
                        continue
                    self._post_publication(
                        "failed",
                        SnapFailure(
                            request_id=request.request_id,
                            config_generation=request.config.generation,
                            purpose=request.purpose,
                            message=f"{type(exc).__name__}: {exc}",
                        ),
                        request.config,
                    )
                    continue
                self._post_publication(
                    "snap_ready",
                    response,
                    request.config,
                )
        except Exception as exc:
            self._post_publication(
                "lifecycle_failed",
                f"{type(exc).__name__}: {exc}",
            )
        finally:
            if backend is not None:
                try:
                    backend.close()
                except Exception as exc:
                    self._post_publication(
                        "lifecycle_failed",
                        f"{type(exc).__name__}: {exc}",
                    )
            self._post_finished()

    def _snap_work(self, request: SnapRequest) -> _SnapWork:
        # Called only while self._condition is held by _take_next().
        return _SnapWork(
            request=request,
            cancellation_generation=self._cancellation_generation,
            hover_cancellation_generation=self._hover_cancellation_generation,
        )

    def _work_is_obsolete(self, work: _SnapWork) -> bool:
        with self._condition:
            if self._stopping or self._stop_requested.is_set():
                return True
            if self._cancellation_generation != work.cancellation_generation:
                return True
            if (
                work.request.purpose == "hover"
                and self._hover_cancellation_generation
                != work.hover_cancellation_generation
            ):
                return True
            if work.request.config != self._latest_config:
                return True
            if work.request.purpose != "hover":
                return False
            if self._clicks:
                return True
            return (
                self._hover is not None
                and self._hover.request_id != work.request.request_id
            )

    def _take_next(self) -> _SnapWork | object:
        with self._condition:
            while True:
                while (
                    not self._clicks
                    and self._hover is None
                    and not self._stopping
                    and not self._stop_requested.is_set()
                ):
                    self._condition.wait()
                if self._stopping or self._stop_requested.is_set():
                    return _STOP_WORKER
                while self._clicks:
                    request = self._clicks.popleft()
                    if request.config == self._latest_config:
                        return self._snap_work(request)
                request = self._hover
                self._hover = None
                if request is not None and request.config == self._latest_config:
                    return self._snap_work(request)

    def _require_creator_thread(self) -> None:
        if threading.get_ident() != self._creator_thread_id:
            raise RuntimeError(_CREATOR_THREAD_ERROR)

    def _post_publication(
        self,
        kind: str,
        value: object,
        config: KLayoutConfig | None = None,
    ) -> None:
        self._publication_posted.emit(
            _Publication(
                kind=kind,
                value=value,
                config_generation=None if config is None else config.generation,
            )
        )

    def _post_finished(self) -> None:
        with self._condition:
            if self._finished_publication_posted:
                return
            self._finished_publication_posted = True
            self._thread_finished_event.set()
        self._finished_posted.emit()

    @property
    def is_finished(self) -> bool:
        return self._thread_finished_event.is_set()

    @Slot(object)
    def _deliver_publication(self, publication: _Publication) -> None:
        with self._condition:
            if self._stopping or self._stop_requested.is_set():
                return
            if publication.config_generation is not None and (
                self._latest_config is None
                or publication.config_generation != self._latest_config.generation
            ):
                return
        getattr(self, publication.kind).emit(publication.value)

    @Slot()
    def _deliver_finished(self) -> None:
        self.finished.emit()


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
                if request is _STOP_WORKER:
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
                return _STOP_WORKER
            request = self._pending
            self._pending = None
            return request

    def _require_creator_thread(self) -> None:
        if threading.get_ident() != self._creator_thread_id:
            raise RuntimeError(_CREATOR_THREAD_ERROR)

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


class _KLayoutRenderBackend:
    """Own the Qt-less KLayout view used by one render worker thread."""

    def __init__(self) -> None:
        self._db: Any = None
        self._view: Any = None
        self._cellview: Any = None
        self._cellview_index: int | None = None
        self._path: Path | None = None
        self._source_load_id: str | None = None

    def ensure_config(self, config: KLayoutConfig) -> None:
        if self._path != config.path or self._source_load_id != config.source_load_id:
            self.close()
            import klayout.db as db
            import klayout.lay as lay

            self._db = db
            self._view = lay.LayoutView()
            self._cellview_index = self._view.load_layout(str(config.path), False)
            self._cellview = self._view.cellview(self._cellview_index)
            self._path = config.path
            self._source_load_id = config.source_load_id

        layout = self._cellview.layout()
        cell = layout.cell(config.top_cell_name)
        if cell is None:
            raise RuntimeError(f"Top cell '{config.top_cell_name}' does not exist")
        self._cellview.set_cell(cell.cell_index())
        self._view.max_hier()
        self._apply_visible_layers(config.visible_layers)

    def render(self, request: RenderRequest) -> RenderFrame:
        started = time.perf_counter()
        source_box = inverse_rotate_box(request.world_box, request.config)
        odd_rotation = request.config.rotation_quarter_turns % 2 == 1
        source_width = request.pixel_height if odd_rotation else request.pixel_width
        source_height = request.pixel_width if odd_rotation else request.pixel_height
        pixels = self._view.get_pixels_with_options(
            source_width,
            source_height,
            1,
            1,
            0,
            self._db.DBox(*source_box),
        )
        png_data = bytes(pixels.to_png_data())
        del pixels
        image = QImage.fromData(png_data, "PNG")
        if image.isNull():
            raise RuntimeError("KLayout returned an invalid PNG frame")
        image = image.copy()
        image = _rotate_image(image, request.config.rotation_quarter_turns)
        if image.width() != request.pixel_width or image.height() != request.pixel_height:
            raise RuntimeError(
                "KLayout frame dimensions do not match the render request"
            )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return RenderFrame(
            request_id=request.request_id,
            config_generation=request.config.generation,
            viewport_generation=request.viewport_generation,
            world_box=request.world_box,
            pixel_width=request.pixel_width,
            pixel_height=request.pixel_height,
            density=request.density,
            image=image,
            elapsed_ms=elapsed_ms,
            purpose=request.purpose,
        )

    def close(self) -> None:
        cellview = self._cellview
        view = self._view
        self._cellview = None
        self._view = None
        self._cellview_index = None
        self._path = None
        self._source_load_id = None
        self._db = None
        del cellview
        if view is not None:
            try:
                view.stop_redraw()
            finally:
                del view

    def _apply_visible_layers(self, visible_layers: frozenset[tuple[int, int]]) -> None:
        iterator = self._view.begin_layers()
        try:
            while not iterator.at_end():
                properties = iterator.current().dup()
                key = (int(properties.source_layer), int(properties.source_datatype))
                properties.visible = key in visible_layers
                self._view.set_layer_properties(iterator, properties)
                iterator.next()
        finally:
            del iterator


class _KLayoutSnapBackend:
    """Own the independent KLayout database used by one snap worker thread."""

    def __init__(self, *, clock: Callable[[], float] = time.perf_counter) -> None:
        self._clock = clock
        self._db: Any = None
        self._layout: Any = None
        self._top_cell: Any = None
        self._layer_indexes: dict[tuple[int, int], int] = {}
        self._path: Path | None = None
        self._source_load_id: str | None = None

    def ensure_config(self, config: KLayoutConfig) -> None:
        if self._path != config.path or self._source_load_id != config.source_load_id:
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

        top_cell = self._layout.cell(config.top_cell_name)
        if top_cell is None:
            raise RuntimeError(f"Top cell '{config.top_cell_name}' does not exist")
        self._top_cell = top_cell

    def snap(
        self,
        request: SnapRequest,
        *,
        is_cancelled: Callable[[], bool],
    ) -> SnapResponse:
        started = self._clock()
        if is_cancelled():
            return self._unavailable_response(
                request,
                started,
                0,
                0,
                SNAP_UNAVAILABLE_CANCELLED,
            )
        plan = plan_snap_search(
            request.point,
            request.radius,
            request.config.display_bounds,
        )
        if not plan.accepted:
            return self._unavailable_response(
                request,
                started,
                0,
                0,
                plan.skip_reason or SNAP_UNAVAILABLE_CANCELLED,
            )
        source_point = inverse_rotate_point(request.point, request.config)
        source_search_box = inverse_rotate_box(plan.search_box, request.config)
        shape_stream = self._iter_shape_contours(request.config, source_search_box)
        try:
            collected = _collect_snap_geometry(
                shape_stream,
                request.budget,
                clock=self._clock,
                is_cancelled=is_cancelled,
            )
        finally:
            close = getattr(shape_stream, "close", None)
            if close is not None:
                close()
        if collected.unavailable_reason is not None:
            return self._unavailable_response(
                request,
                started,
                collected.shapes_inspected,
                collected.candidates_generated,
                collected.unavailable_reason,
            )
        source_result = select_snap(
            source_point,
            collected.vertices,
            collected.segments,
            request.radius,
        )
        result = _rotate_snap_result(source_result, request)
        return SnapResponse(
            request_id=request.request_id,
            config_generation=request.config.generation,
            raw_point=request.point,
            result=result,
            elapsed_ms=(self._clock() - started) * 1000.0,
            shapes_inspected=collected.shapes_inspected,
            purpose=request.purpose,
            candidates_generated=collected.candidates_generated,
        )

    def _unavailable_response(
        self,
        request: SnapRequest,
        started: float,
        shapes: int,
        candidates: int,
        reason: str,
    ) -> SnapResponse:
        return SnapResponse(
            request_id=request.request_id,
            config_generation=request.config.generation,
            raw_point=request.point,
            result=SnapResult(point=request.point, mode="free", distance=0.0),
            elapsed_ms=(self._clock() - started) * 1000.0,
            shapes_inspected=shapes,
            purpose=request.purpose,
            candidates_generated=candidates,
            unavailable_reason=reason,
        )

    def _iter_shape_contours(
        self,
        config: KLayoutConfig,
        source_search_box: Box2D,
    ) -> Iterable[ShapeContours]:
        search_box = self._db.DBox(*source_search_box)
        for layer_key in sorted(config.visible_layers):
            layer_index = self._layer_indexes.get(layer_key)
            if layer_index is None:
                continue
            iterator = self._top_cell.begin_shapes_rec_touching(
                layer_index,
                search_box,
            )
            try:
                while not iterator.at_end():
                    yield _shape_contours(
                        iterator.shape(),
                        iterator.dtrans(),
                        self._db,
                    )
                    iterator.next()
            finally:
                del iterator

    def close(self) -> None:
        top_cell = self._top_cell
        layout = self._layout
        self._top_cell = None
        self._layout = None
        self._layer_indexes = {}
        self._path = None
        self._source_load_id = None
        self._db = None
        del top_cell
        del layout


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
                        for contour, _closed in _shape_contours(
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


def _rotate_image(image: QImage, quarter_turns: int) -> QImage:
    turns = int(quarter_turns) % 4
    if turns == 0:
        return image
    return image.transformed(QTransform().rotate(-90.0 * turns)).copy()


def _rotate_snap_result(result: SnapResult, request: SnapRequest) -> SnapResult:
    if result.mode == "free":
        return SnapResult(point=request.point, mode="free", distance=0.0)
    segment_start = (
        forward_rotate_point(result.segment_start, request.config)
        if result.segment_start is not None
        else None
    )
    segment_end = (
        forward_rotate_point(result.segment_end, request.config)
        if result.segment_end is not None
        else None
    )
    return SnapResult(
        point=forward_rotate_point(result.point, request.config),
        mode=result.mode,
        distance=result.distance,
        segment_start=segment_start,
        segment_end=segment_end,
    )


ShapeContours: TypeAlias = Iterable[tuple[Iterable[Point2D], bool]]


@dataclass(frozen=True)
class _CollectedSnapGeometry:
    vertices: Sequence[Point2D]
    segments: Sequence[Segment2D]
    shapes_inspected: int
    candidates_generated: int
    unavailable_reason: str | None = None


def _collect_snap_geometry(
    shapes: Iterable[ShapeContours],
    budget: SnapWorkBudget,
    *,
    clock: Callable[[], float],
    is_cancelled: Callable[[], bool],
) -> _CollectedSnapGeometry:
    started = clock()
    vertices: list[Point2D] = []
    segments: list[Segment2D] = []
    shapes_inspected = 0
    candidates_generated = 0

    def abort(reason: str) -> _CollectedSnapGeometry:
        return _CollectedSnapGeometry(
            (),
            (),
            shapes_inspected,
            candidates_generated,
            reason,
        )

    def checkpoint() -> str | None:
        if is_cancelled():
            return SNAP_UNAVAILABLE_CANCELLED
        if (clock() - started) * 1_000.0 > budget.max_elapsed_ms:
            return SNAP_UNAVAILABLE_TIME_BUDGET
        return None

    shape_iterator = iter(shapes)
    while True:
        reason = checkpoint()
        if reason is not None:
            return abort(reason)
        if shapes_inspected >= budget.max_shapes:
            return abort(SNAP_UNAVAILABLE_SHAPE_BUDGET)
        try:
            shape_contours = next(shape_iterator)
        except StopIteration:
            break
        shapes_inspected += 1
        contour_iterator = iter(shape_contours)
        while True:
            reason = checkpoint()
            if reason is not None:
                return abort(reason)
            try:
                contour, closed = next(contour_iterator)
            except StopIteration:
                break
            contour_points: list[Point2D] = []
            point_iterator = iter(contour)
            while True:
                reason = checkpoint()
                if reason is not None:
                    return abort(reason)
                try:
                    point = next(point_iterator)
                except StopIteration:
                    break
                added_candidates = 1 + int(bool(contour_points))
                if candidates_generated + added_candidates > budget.max_candidates:
                    return abort(SNAP_UNAVAILABLE_CANDIDATE_BUDGET)
                vertices.append(point)
                candidates_generated += 1
                if contour_points:
                    segments.append((contour_points[-1], point))
                    candidates_generated += 1
                contour_points.append(point)
            if closed and len(contour_points) > 1:
                reason = checkpoint()
                if reason is not None:
                    return abort(reason)
                if candidates_generated + 1 > budget.max_candidates:
                    return abort(SNAP_UNAVAILABLE_CANDIDATE_BUDGET)
                segments.append((contour_points[-1], contour_points[0]))
                candidates_generated += 1
    return _CollectedSnapGeometry(
        tuple(vertices),
        tuple(segments),
        shapes_inspected,
        candidates_generated,
    )


def _point_tuples(points: Iterable[Any]) -> Iterable[Point2D]:
    for point in points:
        yield (float(point.x), float(point.y))


def _shape_contours(
    shape: Any,
    transform: Any,
    db: Any,
) -> ShapeContours:
    if shape.is_box():
        box = shape.dbox
        points = (
            db.DPoint(box.left, box.bottom),
            db.DPoint(box.right, box.bottom),
            db.DPoint(box.right, box.top),
            db.DPoint(box.left, box.top),
        )
        yield (_point_tuples(transform * point for point in points), True)
        return

    if shape.is_polygon():
        polygon = transform * shape.dpolygon
    elif shape.is_path():
        polygon = transform * shape.dpath.polygon()
    elif hasattr(shape, "is_edge") and shape.is_edge():
        edge = shape.dedge
        yield (
            _point_tuples((transform * edge.p1, transform * edge.p2)),
            False,
        )
        return
    else:
        return

    yield (_point_tuples(polygon.each_point_hull()), True)
    for hole_index in range(polygon.holes()):
        yield (_point_tuples(polygon.each_point_hole(hole_index)), True)


__all__ = [
    "KLayoutRenderWorker",
    "KLayoutSnapWorker",
    "KLayoutStructureBoundsWorker",
]
