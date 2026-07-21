"""Background rendering, caching, and worker lifecycle for the minimap."""

from __future__ import annotations

import logging
import math
from pathlib import Path
import threading
from time import perf_counter
from typing import Callable, Protocol

from PySide6.QtCore import QObject, QPoint, QPointF, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPixmap

from probe_station_gui.design.klayout_types import (
    KLayoutConfig,
    RenderFailure,
    RenderFrame,
    RenderRequest,
)
from probe_station_gui.design.klayout_workers import KLayoutRenderWorker
from probe_station_gui.design.model import DesignDocument
from probe_station_gui.views.microscope_minimap_rendering import MinimapRendering

logger = logging.getLogger(__name__)


class LegacyRenderer(Protocol):
    def render(
        self,
        document: DesignDocument,
        size: QSize,
        callback: Callable[[QImage | None], None],
    ) -> None: ...


class ThreadedLegacyRenderer:
    """Render in-memory polygons off the GUI thread and return a QImage."""

    def render(
        self,
        document: DesignDocument,
        size: QSize,
        callback: Callable[[QImage | None], None],
    ) -> None:
        render_size = QSize(size)

        def render_background() -> None:
            started = perf_counter()
            try:
                image, point_count = self.render_image(document, render_size)
            except Exception:
                logger.exception("MINIMAP RENDER failed")
                callback(None)
                return
            logger.debug(
                "MINIMAP RENDER complete points=%d elapsed_ms=%.2f",
                point_count,
                (perf_counter() - started) * 1000.0,
            )
            callback(image)

        threading.Thread(
            target=render_background, name="DesignMinimapRender", daemon=True
        ).start()

    @classmethod
    def render_image(cls, document: DesignDocument, size: QSize) -> tuple[QImage, int]:
        image = QImage(size, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing, False)
        target_rect = QRect(QPoint(0, 0), size)
        left, bottom, right, top = document.bounds
        width = max(right - left, 1e-9)
        height = max(top - bottom, 1e-9)
        pad = 6.0
        usable_width = max(target_rect.width() - 2.0 * pad, 1.0)
        usable_height = max(target_rect.height() - 2.0 * pad, 1.0)
        scale = min(usable_width / width, usable_height / height)
        offset_x = target_rect.left() + (target_rect.width() - width * scale) * 0.5
        offset_y = target_rect.top() + (target_rect.height() - height * scale) * 0.5
        point_count = 0
        for layer_key, polygons in document.visible_polygons().items():
            painter.setPen(QPen(cls._layer_color(layer_key), 1))
            painter.setBrush(Qt.NoBrush)
            for polygon in polygons:
                if len(polygon) < 2:
                    continue
                point_count += int(len(polygon))
                x_values = offset_x + (polygon[:, 0] - left) * scale
                y_values = offset_y + (top - polygon[:, 1]) * scale
                if len(polygon) > 2:
                    pixel_x = x_values.astype(int)
                    pixel_y = y_values.astype(int)
                    keep = pixel_x == pixel_x
                    keep[0] = True
                    keep[1:] = (pixel_x[1:] != pixel_x[:-1]) | (
                        pixel_y[1:] != pixel_y[:-1]
                    )
                    x_values = x_values[keep]
                    y_values = y_values[keep]
                if len(x_values) < 2:
                    continue
                path = QPainterPath()
                path.moveTo(QPointF(float(x_values[0]), float(y_values[0])))
                for x_value, y_value in zip(x_values[1:], y_values[1:]):
                    path.lineTo(QPointF(float(x_value), float(y_value)))
                path.closeSubpath()
                painter.drawPath(path)
        painter.end()
        return image, point_count

    @staticmethod
    def _layer_color(layer_key: tuple[int, int]) -> QColor:
        hue = int((layer_key[0] * 57 + layer_key[1] * 19) % 360)
        return QColor.fromHsv(hue, 120, 145, 180)


class MinimapBackground(QObject):
    """Coordinate real renderers and accept only current background results."""

    changed = Signal()
    _legacy_ready = Signal(int, object, object, object)

    def __init__(
        self,
        *,
        renderer: LegacyRenderer | None = None,
        worker_factory: Callable[[], KLayoutRenderWorker] = KLayoutRenderWorker,
        device_pixel_ratio: Callable[[], float] = lambda: 1.0,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._renderer = renderer or ThreadedLegacyRenderer()
        self._device_pixel_ratio = device_pixel_ratio
        self._design_document: DesignDocument | None = None
        self._document_signature: tuple[object, ...] | None = None
        self._minimap_background: QPixmap | None = None
        self._minimap_cache_key: tuple[object, ...] | None = None
        self._minimap_render_key: tuple[object, ...] | None = None
        self._minimap_render_generation = 0
        self._minimap_render_worker_factory = worker_factory
        self._minimap_render_worker: KLayoutRenderWorker | None = None
        self._minimap_render_worker_source_key: tuple[Path, str | None] | None = None
        self._retired_minimap_workers: set[object] = set()
        self._minimap_retirement_callbacks: dict[object, object] = {}
        self._minimap_klayout_config: KLayoutConfig | None = None
        self._minimap_klayout_signature: tuple[object, ...] | None = None
        self._minimap_klayout_generation = 0
        self._minimap_request_id = 0
        self._minimap_viewport_generation = 0
        self._minimap_latest_request_id: int | None = None
        self._minimap_latest_request: RenderRequest | None = None
        self._minimap_retry_count = 0
        self._minimap_failed_key: tuple[object, ...] | None = None
        self._minimap_desired_key: tuple[object, ...] | None = None
        self._minimap_pending_render: tuple[QSize, tuple[object, ...]] | None = None
        self._minimap_background_config_generation: int | None = None
        self._minimap_renderer_shutdown = False
        self._minimap_render_timer = QTimer(self)
        self._minimap_render_timer.setSingleShot(True)
        self._minimap_render_timer.timeout.connect(self._flush_klayout_minimap_render)
        self._legacy_ready.connect(self._on_minimap_background_ready)

    def configure(self, document: DesignDocument | object | None) -> int:
        valid_document = document if isinstance(document, DesignDocument) else None
        signature = self._render_signature(valid_document)
        if (
            valid_document is self._design_document
            and signature == self._document_signature
        ):
            return self._minimap_render_generation
        if valid_document is not self._design_document:
            self._minimap_klayout_signature = None
        self._configure_minimap_document(valid_document)
        self._design_document = valid_document
        self._document_signature = signature
        return self._minimap_render_generation

    def background_for_size(self, size: QSize) -> QPixmap | None:
        return self._design_background_for_size(size)

    @staticmethod
    def _render_signature(
        document: DesignDocument | None,
    ) -> tuple[object, ...] | None:
        if document is None:
            return None
        return (
            Path(document.path).expanduser().resolve(),
            document.source_load_id,
            document.top_cell_name,
            frozenset(document.visible_layers),
            tuple(document.bounds),
            tuple(document.cell_bounds.get(document.top_cell_name, document.bounds)),
            int(document.rotation_quarter_turns) % 4,
            bool(document.file_backed),
        )

    def _configure_minimap_document(
        self,
        document: DesignDocument | None,
    ) -> None:
        """Update file-backed render state without starting the lazy worker."""

        if document is None:
            self._minimap_klayout_generation += 1
            self._minimap_klayout_config = None
            self._minimap_klayout_signature = None
            self._invalidate_minimap_background()
            self._stop_minimap_render_worker(timeout_s=0.0)
            return
        if not document.file_backed:
            self._minimap_klayout_generation += 1
            self._minimap_klayout_config = None
            self._minimap_klayout_signature = None
            self._invalidate_minimap_background()
            self._stop_minimap_render_worker(timeout_s=0.0)
            return

        resolved_path = Path(document.path).expanduser().resolve()
        source_bounds = tuple(document.cell_bounds[document.top_cell_name])
        display_bounds = tuple(document.bounds)
        signature = (
            resolved_path,
            document.source_load_id,
            document.top_cell_name,
            frozenset(document.visible_layers),
            source_bounds,
            display_bounds,
            int(document.rotation_quarter_turns) % 4,
        )
        if (
            self._minimap_render_worker is not None
            and self._minimap_render_worker_source_key
            != (resolved_path, document.source_load_id)
        ):
            self._stop_minimap_render_worker(timeout_s=0.0)
        if (
            signature == self._minimap_klayout_signature
            and self._minimap_klayout_config is not None
        ):
            return

        self._minimap_klayout_generation += 1
        self._minimap_klayout_signature = signature
        self._minimap_klayout_config = KLayoutConfig(
            path=resolved_path,
            top_cell_name=document.top_cell_name,
            visible_layers=frozenset(document.visible_layers),
            source_bounds=source_bounds,
            display_bounds=display_bounds,
            rotation_quarter_turns=document.rotation_quarter_turns,
            generation=self._minimap_klayout_generation,
            source_load_id=document.source_load_id,
        )
        self._invalidate_minimap_background()

    def _invalidate_minimap_background(self) -> None:
        self._minimap_render_generation += 1
        self._minimap_render_timer.stop()
        self._minimap_pending_render = None
        self._minimap_desired_key = None
        self._minimap_render_key = None
        self._minimap_latest_request_id = None
        self._minimap_latest_request = None
        self._minimap_retry_count = 0
        self._minimap_failed_key = None
        self._minimap_cache_key = None
        self._minimap_background = None
        self._minimap_background_config_generation = None

    def _stop_minimap_render_worker(self, *, timeout_s: float) -> None:
        # A retired worker may still finish asynchronously after a document swap.
        # Keep it alive until its creator-thread completion signal arrives so Qt
        # object destruction never races the worker thread.
        worker = self._minimap_render_worker
        self._minimap_render_worker = None
        self._minimap_render_worker_source_key = None
        if worker is None:
            return
        try:
            worker.frame_ready.disconnect(self._on_klayout_minimap_frame)
        except (RuntimeError, TypeError):
            pass
        try:
            worker.failed.disconnect(self._on_klayout_minimap_failed)
        except (RuntimeError, TypeError):
            pass
        finished = getattr(worker, "finished", None)
        if finished is None:
            worker.stop(timeout_s=timeout_s)
            worker.deleteLater()
            return

        def callback(worker: object = worker) -> None:
            self._finalize_retired_minimap_worker(worker)

        self._retired_minimap_workers.add(worker)
        self._minimap_retirement_callbacks[worker] = callback
        finished.connect(callback)
        worker.stop(timeout_s=timeout_s)
        if bool(getattr(worker, "is_finished", False)):
            self._finalize_retired_minimap_worker(worker)

    def _finalize_retired_minimap_worker(self, worker: object) -> None:
        if worker not in self._retired_minimap_workers:
            return
        callback = self._minimap_retirement_callbacks.pop(worker, None)
        finished = getattr(worker, "finished", None)
        if callback is not None and finished is not None:
            try:
                finished.disconnect(callback)
            except (RuntimeError, TypeError):
                pass
        self._retired_minimap_workers.discard(worker)
        worker.deleteLater()

    def shutdown(self) -> None:
        """Detach the minimap worker on its creator thread without waiting."""

        if self._minimap_renderer_shutdown:
            return
        self._minimap_renderer_shutdown = True
        self._minimap_klayout_generation += 1
        self._minimap_klayout_config = None
        self._minimap_klayout_signature = None
        self._invalidate_minimap_background()
        self._stop_minimap_render_worker(timeout_s=0.0)

    def _design_background_for_size(self, size: QSize) -> QPixmap | None:
        if self._minimap_renderer_shutdown or self._design_document is None:
            return None
        if self._design_document.file_backed:
            return self._klayout_background_for_size(size)
        cache_key = (
            id(self._design_document),
            int(size.width()),
            int(size.height()),
        )
        if (
            self._minimap_cache_key == cache_key
            and self._minimap_background is not None
        ):
            return self._minimap_background

        self._start_minimap_background_render(size, cache_key)
        return None

    def _klayout_background_for_size(self, size: QSize) -> QPixmap | None:
        config = self._minimap_klayout_config
        if config is None or self._minimap_renderer_shutdown:
            return None
        logical_width = int(size.width())
        logical_height = int(size.height())
        if logical_width <= 0 or logical_height <= 0:
            return None
        fitted_rect = MinimapRendering.design_rect_for_bounds(
            QRect(QPoint(0, 0), size),
            config.display_bounds,
        )
        pixel_width, pixel_height = self._minimap_physical_size(
            fitted_rect.width(),
            fitted_rect.height(),
        )
        cache_key = (
            "klayout",
            config.generation,
            logical_width,
            logical_height,
            pixel_width,
            pixel_height,
        )
        self._minimap_desired_key = cache_key
        if (
            self._minimap_failed_key is not None
            and self._minimap_failed_key != cache_key
        ):
            self._minimap_failed_key = None
        if (
            self._minimap_cache_key == cache_key
            and self._minimap_background is not None
        ):
            self._minimap_pending_render = None
            self._minimap_render_timer.stop()
            return self._minimap_background
        if self._minimap_failed_key == cache_key:
            self._minimap_pending_render = None
            self._minimap_render_timer.stop()
            if (
                self._minimap_background is not None
                and self._minimap_background_config_generation == config.generation
            ):
                return self._minimap_background
            return None
        if self._minimap_render_key == cache_key:
            self._minimap_pending_render = None
            self._minimap_render_timer.stop()
        else:
            self._minimap_pending_render = (QSize(size), cache_key)
            self._minimap_render_timer.start(0)
        if (
            self._minimap_background is not None
            and self._minimap_background_config_generation == config.generation
        ):
            return self._minimap_background
        return None

    def _minimap_physical_size(
        self,
        logical_width: float,
        logical_height: float,
    ) -> tuple[int, int]:
        try:
            device_scale = float(self._device_pixel_ratio())
        except Exception:
            device_scale = 1.0
        if not math.isfinite(device_scale) or device_scale <= 0.0:
            device_scale = 1.0
        return (
            max(1, round(float(logical_width) * device_scale)),
            max(1, round(float(logical_height) * device_scale)),
        )

    def _flush_klayout_minimap_render(self) -> None:
        pending = self._minimap_pending_render
        config = self._minimap_klayout_config
        self._minimap_pending_render = None
        if pending is None or config is None or self._minimap_renderer_shutdown:
            return
        _logical_size, cache_key = pending
        if self._minimap_desired_key != cache_key or cache_key[1] != config.generation:
            return

        worker = self._minimap_render_worker
        source_key = (config.path, config.source_load_id)
        if worker is None or self._minimap_render_worker_source_key != source_key:
            if worker is not None:
                self._stop_minimap_render_worker(timeout_s=0.0)
            worker = self._minimap_render_worker_factory()
            self._minimap_render_worker = worker
            self._minimap_render_worker_source_key = source_key
            worker.frame_ready.connect(self._on_klayout_minimap_frame)
            worker.failed.connect(self._on_klayout_minimap_failed)
        self._submit_klayout_minimap_request(worker, config, cache_key, retry_count=0)

    def _submit_klayout_minimap_request(
        self,
        worker: KLayoutRenderWorker,
        config: KLayoutConfig,
        cache_key: tuple[object, ...],
        *,
        retry_count: int,
    ) -> None:
        pixel_width = int(cache_key[4])
        pixel_height = int(cache_key[5])
        left, bottom, right, top = config.display_bounds
        world_width = max(float(right) - float(left), 1e-12)
        world_height = max(float(top) - float(bottom), 1e-12)
        density = min(pixel_width / world_width, pixel_height / world_height)
        self._minimap_request_id += 1
        self._minimap_viewport_generation += 1
        request = RenderRequest(
            request_id=self._minimap_request_id,
            config=config,
            world_box=config.display_bounds,
            pixel_width=pixel_width,
            pixel_height=pixel_height,
            viewport_generation=self._minimap_viewport_generation,
            density=float(density),
            purpose="minimap",
        )
        self._minimap_latest_request_id = request.request_id
        self._minimap_latest_request = request
        self._minimap_retry_count = int(retry_count)
        self._minimap_render_key = cache_key
        logger.debug(
            "KLayout minimap render scheduled request=%d size=%dx%d document=%s",
            request.request_id,
            pixel_width,
            pixel_height,
            config.path.name,
        )
        worker.submit(request)

    def _on_klayout_minimap_frame(self, frame: object) -> None:
        config = self._minimap_klayout_config
        cache_key = self._minimap_render_key
        if (
            config is None
            or cache_key is None
            or not isinstance(frame, RenderFrame)
            or frame.request_id != self._minimap_latest_request_id
            or frame.config_generation != config.generation
            or frame.viewport_generation != self._minimap_viewport_generation
            or frame.purpose != "minimap"
            or tuple(frame.world_box) != tuple(config.display_bounds)
            or frame.pixel_width != int(cache_key[4])
            or frame.pixel_height != int(cache_key[5])
            or self._minimap_desired_key != cache_key
            or not isinstance(frame.image, QImage)
            or frame.image.isNull()
        ):
            return
        self._minimap_render_key = None
        self._minimap_latest_request = None
        self._minimap_cache_key = cache_key
        self._minimap_background_config_generation = config.generation
        self._minimap_background = QPixmap.fromImage(frame.image)
        self._minimap_failed_key = None
        logger.debug(
            "KLayout minimap render accepted request=%d elapsed_ms=%.2f",
            frame.request_id,
            frame.elapsed_ms,
        )
        self.changed.emit()

    def _on_klayout_minimap_failed(self, failure: object) -> None:
        config = self._minimap_klayout_config
        request = self._minimap_latest_request
        cache_key = self._minimap_render_key
        if (
            config is None
            or request is None
            or cache_key is None
            or not isinstance(failure, RenderFailure)
            or failure.request_id != request.request_id
            or failure.config_generation != config.generation
            or failure.viewport_generation != request.viewport_generation
            or failure.purpose != "minimap"
            or self._minimap_desired_key != cache_key
        ):
            return
        self._minimap_render_key = None
        self._minimap_latest_request_id = None
        self._minimap_latest_request = None
        logger.error("KLayout minimap render failed: %s", failure.message)
        worker = self._minimap_render_worker
        if worker is None or self._minimap_retry_count >= 1:
            self._minimap_failed_key = cache_key
            return
        self._submit_klayout_minimap_request(
            worker,
            config,
            cache_key,
            retry_count=self._minimap_retry_count + 1,
        )

    def _start_minimap_background_render(
        self,
        size: QSize,
        cache_key: tuple[object, ...],
    ) -> None:
        if (
            self._minimap_renderer_shutdown
            or self._design_document is None
            or self._design_document.file_backed
        ):
            return
        if self._minimap_render_key == cache_key:
            return
        document = self._design_document
        generation = self._minimap_render_generation
        render_size = QSize(size)
        self._minimap_render_key = cache_key
        logger.debug(
            "MINIMAP RENDER scheduled size=%dx%d document=%s",
            render_size.width(),
            render_size.height(),
            document.path.name,
        )

        def accept_image(image: QImage | None) -> None:
            self._legacy_ready.emit(generation, cache_key, render_size, image)

        self._renderer.render(document, render_size, accept_image)

    def _on_minimap_background_ready(
        self,
        generation: int,
        cache_key: object,
        _size: object,
        image: object,
    ) -> None:
        if self._design_document is None or self._design_document.file_backed:
            return
        if generation != self._minimap_render_generation:
            return
        if cache_key != self._minimap_render_key:
            return
        self._minimap_render_key = None
        if not isinstance(cache_key, tuple) or not isinstance(image, QImage):
            return
        self._minimap_cache_key = cache_key
        self._minimap_background = QPixmap.fromImage(image)
        self._minimap_background_config_generation = None
        self.changed.emit()


__all__ = ["LegacyRenderer", "MinimapBackground", "ThreadedLegacyRenderer"]
