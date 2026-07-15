"""Worker-backed KLayout raster item and pyqtgraph viewport scheduler."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QRectF, QTimer, Signal
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QGraphicsObject

from probe_station_gui.design.klayout_types import (
    Box2D,
    KLayoutConfig,
    RenderFailure,
    RenderFrame,
    RenderRequest,
)
from probe_station_gui.design.klayout_workers import KLayoutRenderWorker
from probe_station_gui.design.model import DesignDocument


MAX_RENDER_DIMENSION_PX = 4096
ACTIVE_SCHEDULE_MS = 16
SETTLE_SCHEDULE_MS = 120
PAN_GUARD_FRACTION = 0.04
SETTLED_MARGIN_FRACTION = 0.125
RASTER_Z_VALUE = -10_000.0
_MIN_WORLD_SPAN = 1e-12


class KLayoutRasterItem(QGraphicsObject):
    """One image whose bounding rectangle is its exact design-space world box."""

    def __init__(self, parent: QGraphicsObject | None = None) -> None:
        super().__init__(parent)
        self._frame: RenderFrame | None = None
        self._bounds = QRectF()
        self.setZValue(RASTER_Z_VALUE)

    @property
    def frame(self) -> RenderFrame | None:
        return self._frame

    def boundingRect(self) -> QRectF:  # noqa: N802 - Qt API
        return QRectF(self._bounds)

    def set_frame(self, frame: RenderFrame) -> None:
        """Replace the image and its exact design-space placement atomically."""

        left, bottom, right, top = frame.world_box
        bounds = QRectF(
            float(left),
            float(bottom),
            float(right) - float(left),
            float(top) - float(bottom),
        )
        self.prepareGeometryChange()
        self._frame = frame
        self._bounds = bounds
        self.update()

    def clear(self) -> None:
        if self._frame is None and self._bounds.isNull():
            return
        self.prepareGeometryChange()
        self._frame = None
        self._bounds = QRectF()
        self.update()

    def paint(self, painter: QPainter, _option, _widget=None) -> None:
        frame = self._frame
        if frame is None or not isinstance(frame.image, QImage) or frame.image.isNull():
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        painter.save()
        painter.translate(0.0, self._bounds.top() + self._bounds.bottom())
        painter.scale(1.0, -1.0)
        painter.drawImage(self._bounds, frame.image)
        painter.restore()


class KLayoutRasterController(QObject):
    """Schedule newest-only viewport frames for one file-backed document."""

    failed = Signal(object)
    succeeded = Signal()

    def __init__(
        self,
        view_box: object,
        raster_item: KLayoutRasterItem,
        *,
        render_worker_factory: Callable[[], KLayoutRenderWorker] = KLayoutRenderWorker,
        device_pixel_ratio: Callable[[], float] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._view_box = view_box
        self._raster_item = raster_item
        self._render_worker_factory = render_worker_factory
        self._device_pixel_ratio = device_pixel_ratio or (lambda: 1.0)
        self._worker: KLayoutRenderWorker | None = None
        self._worker_source_key: tuple[Path, str | None] | None = None
        self._retired_workers: set[QObject] = set()
        self._retirement_callbacks: dict[QObject, Callable[[], None]] = {}
        self._config: KLayoutConfig | None = None
        self._config_generation = 0
        self._request_id = 0
        self._viewport_generation = 0
        self._last_range_box: Box2D | None = self._current_viewport()
        self._pending_zoom = False
        self._shutdown = False
        self._latest_request: RenderRequest | None = None
        self._latest_retry_count = 0

        self._active_timer = QTimer(self)
        self._active_timer.setSingleShot(True)
        self._active_timer.setInterval(ACTIVE_SCHEDULE_MS)
        self._active_timer.timeout.connect(self._flush_active_request)
        self._settle_timer = QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.setInterval(SETTLE_SCHEDULE_MS)
        self._settle_timer.timeout.connect(self.request_settled)
        range_signal = getattr(self._view_box, "sigRangeChanged", None)
        if range_signal is not None:
            range_signal.connect(self._on_range_changed)

    @property
    def config(self) -> KLayoutConfig | None:
        return self._config

    @property
    def raster_item(self) -> KLayoutRasterItem:
        return self._raster_item

    def set_document(self, document: DesignDocument | None) -> None:
        """Configure or detach the file-backed renderer on its creator thread."""

        if self._shutdown:
            return
        if document is None:
            self._config_generation += 1
            self._config = None
            self._pending_zoom = False
            self._active_timer.stop()
            self._settle_timer.stop()
            self._stop_worker(timeout_s=0.5)
            self._raster_item.clear()
            return
        if not document.file_backed:
            raise ValueError("KLayout raster rendering requires a file-backed document")

        resolved_path = Path(document.path).expanduser().resolve()
        source_key = (resolved_path, document.source_load_id)
        if self._worker is None or self._worker_source_key != source_key:
            self._stop_worker(timeout_s=0.0)
            self._worker = self._render_worker_factory()
            self._worker_source_key = source_key
            self._worker.frame_ready.connect(self._on_frame_ready)
            self._worker.failed.connect(self._on_render_failed)
            self._raster_item.clear()

        self._config_generation += 1
        self._config = KLayoutConfig(
            path=resolved_path,
            top_cell_name=document.top_cell_name,
            visible_layers=frozenset(document.visible_layers),
            source_bounds=tuple(document.cell_bounds[document.top_cell_name]),
            display_bounds=tuple(document.bounds),
            rotation_quarter_turns=document.rotation_quarter_turns,
            generation=self._config_generation,
            source_load_id=document.source_load_id,
        )
        self._viewport_generation += 1
        self._last_range_box = self._current_viewport()
        self._pending_zoom = False
        self._active_timer.stop()
        self._settle_timer.stop()
        self._raster_item.clear()
        self.request_exact()

    def request_exact(self) -> None:
        """Request the current viewport at physical-device-pixel density."""

        viewport = self._current_viewport()
        if viewport is None:
            return
        self._submit(viewport, purpose="exact")

    def request_settled(self) -> None:
        """Request a 12.5% margin frame at matching viewport density."""

        viewport = self._current_viewport()
        if viewport is None:
            return
        self._submit(
            _expanded_box(viewport, SETTLED_MARGIN_FRACTION),
            purpose="settled",
        )

    def shutdown(self) -> None:
        """Stop timers and the render worker with a bounded creator-thread join."""

        if self._shutdown:
            return
        self._shutdown = True
        self._active_timer.stop()
        self._settle_timer.stop()
        range_signal = getattr(self._view_box, "sigRangeChanged", None)
        if range_signal is not None:
            try:
                range_signal.disconnect(self._on_range_changed)
            except (RuntimeError, TypeError):
                pass
        self._config_generation += 1
        self._config = None
        self._stop_worker(timeout_s=0.5)
        self._raster_item.clear()

    def _stop_worker(self, *, timeout_s: float) -> None:
        worker = self._worker
        self._worker = None
        self._worker_source_key = None
        if worker is None:
            return
        try:
            worker.frame_ready.disconnect(self._on_frame_ready)
        except (RuntimeError, TypeError):
            pass
        try:
            worker.failed.disconnect(self._on_render_failed)
        except (RuntimeError, TypeError):
            pass
        finished = getattr(worker, "finished", None)
        if finished is None:
            worker.stop(timeout_s=timeout_s)
            worker.deleteLater()
            return
        def callback(worker: QObject = worker) -> None:
            self._finalize_retired_worker(worker)

        self._retired_workers.add(worker)
        self._retirement_callbacks[worker] = callback
        finished.connect(callback)
        worker.stop(timeout_s=timeout_s)
        if bool(getattr(worker, "is_finished", False)):
            self._finalize_retired_worker(worker)

    def _finalize_retired_worker(self, worker: QObject) -> None:
        if worker not in self._retired_workers:
            return
        callback = self._retirement_callbacks.pop(worker, None)
        finished = getattr(worker, "finished", None)
        if callback is not None and finished is not None:
            try:
                finished.disconnect(callback)
            except (RuntimeError, TypeError):
                pass
        self._retired_workers.discard(worker)
        worker.deleteLater()

    def _on_range_changed(self, *_unused: object) -> None:
        if self._config is None or self._shutdown:
            return
        viewport = self._current_viewport()
        if viewport is None:
            return
        previous = self._last_range_box
        if previous is not None and not _same_span(previous, viewport):
            self._pending_zoom = True
        self._last_range_box = viewport
        self._viewport_generation += 1
        if not self._active_timer.isActive():
            self._active_timer.start()
        self._settle_timer.start()

    def _flush_active_request(self) -> None:
        viewport = self._current_viewport()
        if viewport is None or self._config is None:
            self._pending_zoom = False
            return
        frame = self._raster_item.frame
        guarded = _expanded_box(viewport, PAN_GUARD_FRACTION)
        if self._pending_zoom or frame is None or not _box_covers(frame.world_box, guarded):
            self.request_exact()
        self._pending_zoom = False

    def _submit(
        self,
        world_box: Box2D,
        *,
        purpose: str,
        retry_count: int = 0,
    ) -> None:
        config = self._config
        worker = self._worker
        viewport = self._current_viewport()
        if config is None or worker is None or viewport is None:
            return
        pixel_width, pixel_height = self._pixel_dimensions(viewport, world_box)
        world_width = max(_MIN_WORLD_SPAN, float(world_box[2]) - float(world_box[0]))
        world_height = max(_MIN_WORLD_SPAN, float(world_box[3]) - float(world_box[1]))
        density = min(pixel_width / world_width, pixel_height / world_height)
        self._request_id += 1
        request = RenderRequest(
            request_id=self._request_id,
            config=config,
            world_box=world_box,
            pixel_width=pixel_width,
            pixel_height=pixel_height,
            viewport_generation=self._viewport_generation,
            density=float(density),
            purpose=purpose,
        )
        self._latest_request = request
        self._latest_retry_count = int(retry_count)
        worker.submit(request)

    def _pixel_dimensions(self, viewport: Box2D, world_box: Box2D) -> tuple[int, int]:
        scene_rect = self._view_box.sceneBoundingRect()
        logical_width = max(1.0, float(scene_rect.width()))
        logical_height = max(1.0, float(scene_rect.height()))
        try:
            device_scale = float(self._device_pixel_ratio())
        except Exception:
            device_scale = 1.0
        if not math.isfinite(device_scale) or device_scale <= 0.0:
            device_scale = 1.0
        viewport_width = max(_MIN_WORLD_SPAN, viewport[2] - viewport[0])
        viewport_height = max(_MIN_WORLD_SPAN, viewport[3] - viewport[1])
        world_width = max(_MIN_WORLD_SPAN, world_box[2] - world_box[0])
        world_height = max(_MIN_WORLD_SPAN, world_box[3] - world_box[1])
        width = max(1, round(logical_width * device_scale * world_width / viewport_width))
        height = max(
            1,
            round(logical_height * device_scale * world_height / viewport_height),
        )
        reduction = min(1.0, MAX_RENDER_DIMENSION_PX / float(max(width, height)))
        return (
            max(1, min(MAX_RENDER_DIMENSION_PX, round(width * reduction))),
            max(1, min(MAX_RENDER_DIMENSION_PX, round(height * reduction))),
        )

    def _current_viewport(self) -> Box2D | None:
        try:
            view_range = self._view_box.viewRange()
            x_range, y_range = view_range[:2]
            values = (
                float(x_range[0]),
                float(y_range[0]),
                float(x_range[1]),
                float(y_range[1]),
            )
        except (AttributeError, IndexError, TypeError, ValueError):
            return None
        if not all(math.isfinite(value) for value in values):
            return None
        if values[2] - values[0] <= _MIN_WORLD_SPAN:
            return None
        if values[3] - values[1] <= _MIN_WORLD_SPAN:
            return None
        return values

    def _on_frame_ready(self, frame: RenderFrame) -> None:
        config = self._config
        if config is None or frame.config_generation != config.generation:
            return
        if frame.viewport_generation != self._viewport_generation:
            viewport = self._current_viewport()
            displayed = self._raster_item.frame
            displayed_density = 0.0 if displayed is None else float(displayed.density)
            if (
                viewport is None
                or not _box_covers(frame.world_box, viewport)
                or float(frame.density) + 1e-12 < displayed_density
            ):
                return
        self._raster_item.set_frame(frame)
        self.succeeded.emit()

    def _on_render_failed(self, failure: object) -> None:
        request = self._latest_request
        config = self._config
        if (
            request is None
            or config is None
            or not isinstance(failure, RenderFailure)
            or failure.request_id != request.request_id
            or failure.config_generation != config.generation
            or failure.viewport_generation != request.viewport_generation
            or failure.purpose != request.purpose
        ):
            return
        self.failed.emit(failure)
        if self._latest_retry_count >= 1:
            return
        self._submit(
            request.world_box,
            purpose=request.purpose,
            retry_count=self._latest_retry_count + 1,
        )


def _expanded_box(box: Box2D, fraction: float) -> Box2D:
    left, bottom, right, top = box
    x_margin = (float(right) - float(left)) * float(fraction)
    y_margin = (float(top) - float(bottom)) * float(fraction)
    return (
        float(left) - x_margin,
        float(bottom) - y_margin,
        float(right) + x_margin,
        float(top) + y_margin,
    )


def _box_covers(outer: Box2D, inner: Box2D) -> bool:
    epsilon = 1e-9 * max(
        1.0,
        *(abs(float(value)) for value in (*outer, *inner)),
    )
    return (
        float(outer[0]) <= float(inner[0]) + epsilon
        and float(outer[1]) <= float(inner[1]) + epsilon
        and float(outer[2]) + epsilon >= float(inner[2])
        and float(outer[3]) + epsilon >= float(inner[3])
    )


def _same_span(first: Box2D, second: Box2D) -> bool:
    return math.isclose(
        first[2] - first[0],
        second[2] - second[0],
        rel_tol=1e-9,
        abs_tol=_MIN_WORLD_SPAN,
    ) and math.isclose(
        first[3] - first[1],
        second[3] - second[1],
        rel_tol=1e-9,
        abs_tol=_MIN_WORLD_SPAN,
    )


__all__ = [
    "ACTIVE_SCHEDULE_MS",
    "KLayoutRasterController",
    "KLayoutRasterItem",
    "MAX_RENDER_DIMENSION_PX",
    "PAN_GUARD_FRACTION",
    "SETTLE_SCHEDULE_MS",
    "SETTLED_MARGIN_FRACTION",
]
