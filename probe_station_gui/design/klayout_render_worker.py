"""Coalescing KLayout raster worker and its thread-owned render backend."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import threading
import time
from typing import Any, Protocol

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QImage, QTransform

from .klayout_types import (
    KLayoutConfig,
    RenderFailure,
    RenderFrame,
    RenderRequest,
    inverse_rotate_box,
)
from .klayout_worker_runtime import (
    CREATOR_THREAD_ERROR,
    STOP_WORKER,
    WorkerPublication,
)


class _RenderBackend(Protocol):
    def ensure_config(self, config: KLayoutConfig) -> None: ...

    def render(self, request: RenderRequest) -> RenderFrame: ...

    def close(self) -> None: ...


RenderBackendFactory = Callable[[], _RenderBackend]


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
                if request is STOP_WORKER:
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
        config: KLayoutConfig | None = None,
    ) -> None:
        self._publication_posted.emit(
            WorkerPublication(
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
    def _deliver_publication(self, publication: WorkerPublication) -> None:
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


def _rotate_image(image: QImage, quarter_turns: int) -> QImage:
    turns = int(quarter_turns) % 4
    if turns == 0:
        return image
    return image.transformed(QTransform().rotate(-90.0 * turns)).copy()


__all__ = ["KLayoutRenderWorker"]
