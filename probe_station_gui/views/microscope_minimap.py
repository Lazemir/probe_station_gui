"""Design minimap state, rendering, and coordinate mapping."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
from pathlib import Path
import threading
from time import perf_counter
from typing import Callable

from PySide6.QtCore import (
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QPixmap,
)
from PySide6.QtWidgets import QApplication

from probe_station_gui.design.klayout_types import (
    KLayoutConfig,
    RenderFailure,
    RenderFrame,
    RenderRequest,
)
from probe_station_gui.design.klayout_workers import KLayoutRenderWorker
from probe_station_gui.design.model import DesignDocument, MeasurementTarget
from probe_station_gui.route.model import MeasurementRoute

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MinimapBackgroundCacheKey:
    """Identity of an accepted direct background render."""

    document_id: str
    generation: int


class MicroscopeMinimap(QObject):
    """Own design-minimap rendering and interaction state."""

    changed = Signal()
    clicked = Signal(float, float)
    _background_ready = Signal(int, object, object, object)

    _MINIMAP_MARGIN = 16
    _MINIMAP_MIN_SIZE = 160
    _MINIMAP_MAX_SIZE = 240
    _MINIMAP_CLICK_DELAY_PADDING_MS = 50
    _PROBE_ROUTE_DETAIL_POINT_LIMIT = 300
    _PROBE_ROUTE_LABEL_POINT_LIMIT = 150

    def __init__(
        self,
        *,
        renderer: object = KLayoutRenderWorker,
        device_pixel_ratio: Callable[[], float] = lambda: 1.0,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._renderer = renderer
        self._device_pixel_ratio = device_pixel_ratio
        self._design_document: DesignDocument | None = None
        self._design_bounds = (0.0, 0.0, 1.0, 1.0)
        self._design_targets: list[MeasurementTarget] = []
        self._selected_target_id: str | None = None
        self._probe_route: MeasurementRoute | None = None
        self._probe_route_snapshot: tuple[object, ...] | None = None
        self._probe_route_snapshot_token: tuple[object, ...] | None = None
        self._selected_route_point_index = -1
        self._selected_design_point: tuple[float, float] | None = None
        self._current_design_position: tuple[float, float] | None = None
        self._fov_design_size: tuple[float, float] | None = None
        self._source_design_marks: list[tuple[float, float]] = []
        self._check_design_marks: list[tuple[float, float]] = []
        self._minimap_background: QPixmap | None = None
        self._minimap_cache_key: tuple[object, ...] | None = None
        self._minimap_static_overlay: QPixmap | None = None
        self._minimap_static_overlay_key: tuple[object, ...] | None = None
        self._minimap_render_key: tuple[object, ...] | None = None
        self._minimap_render_generation = 0
        self._minimap_render_worker_factory = renderer
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
        self._minimap_rect: QRect | None = None
        self._pending_minimap_click_point: QPoint | None = None
        self._pending_minimap_display_rect: QRect | None = None
        self._background_ready.connect(self._on_minimap_background_ready)
        self._minimap_click_timer = QTimer(self)
        self._minimap_click_timer.setSingleShot(True)
        self._minimap_click_timer.timeout.connect(self._emit_pending_minimap_click)
        self._generation = 0
        self._document_id = ""
        self.background_cache_key: MinimapBackgroundCacheKey | None = None

    def configure(
        self,
        document: DesignDocument | object | None,
        bounds: tuple[float, float, float, float],
        *,
        targets: tuple[MeasurementTarget, ...] | list[MeasurementTarget] = (),
        selected_target_id: str | None = None,
        probe_route: MeasurementRoute | None = None,
        selected_route_point_index: int = -1,
        selected_design_point: tuple[float, float] | None = None,
        current_design_position: tuple[float, float] | None = None,
        fov_design_size: tuple[float, float] | None = None,
        source_design_marks: tuple[tuple[float, float], ...]
        | list[tuple[float, float]] = (),
        check_design_marks: tuple[tuple[float, float], ...]
        | list[tuple[float, float]] = (),
    ) -> int:
        """Update the camera-corner minimap state."""

        self._generation += 1
        self._document_id = str(
            getattr(
                document,
                "document_id",
                getattr(document, "source_load_id", id(document)),
            )
        )
        self._design_bounds = tuple(float(value) for value in bounds)
        self.background_cache_key = None
        if document is not None and not isinstance(document, DesignDocument):
            self._design_document = None
            self._minimap_background = None
            self.changed.emit()
            return self._generation
        if document is not self._design_document:
            self._minimap_static_overlay = None
            self._minimap_static_overlay_key = None
            self._configure_minimap_document(document)
            self._probe_route_snapshot_token = None
        previous_state = (
            id(self._design_document),
            tuple(target.id for target in self._design_targets),
            self._selected_target_id,
            self._probe_route_snapshot,
            self._selected_route_point_index,
            self._selected_design_point,
            self._current_design_position,
            self._fov_design_size,
            tuple(self._source_design_marks),
            tuple(self._check_design_marks),
        )
        self._design_document = document
        self._design_targets = list(targets)
        self._selected_target_id = selected_target_id
        self._probe_route = probe_route
        current_route_snapshot = self._route_snapshot(probe_route)
        self._probe_route_snapshot = current_route_snapshot
        self._selected_route_point_index = selected_route_point_index
        self._selected_design_point = selected_design_point
        self._current_design_position = current_design_position
        self._fov_design_size = fov_design_size
        self._source_design_marks = list(source_design_marks)
        self._check_design_marks = list(check_design_marks)
        current_state = (
            id(self._design_document),
            tuple(target.id for target in self._design_targets),
            self._selected_target_id,
            current_route_snapshot,
            self._selected_route_point_index,
            self._selected_design_point,
            self._current_design_position,
            self._fov_design_size,
            tuple(self._source_design_marks),
            tuple(self._check_design_marks),
        )
        if current_state != previous_state:
            self.changed.emit()
        return self._generation

    def accept_background(self, generation: int, background: QPixmap) -> None:
        """Accept only a direct-render result for the latest configuration."""

        if generation != self._generation:
            return
        self._minimap_background = QPixmap(background)
        self.background_cache_key = MinimapBackgroundCacheKey(
            document_id=self._document_id,
            generation=generation,
        )

    def background_pixel(self, x: int, y: int) -> QColor:
        """Return a cached pixel for renderer-seam tests."""

        if self._minimap_background is None:
            return QColor()
        return self._minimap_background.toImage().pixelColor(x, y)

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

    def draw(self, painter: QPainter, display_rect: QRect) -> None:
        if self._design_document is None:
            return
        size = int(min(display_rect.width(), display_rect.height()) * 0.24)
        size = max(self._MINIMAP_MIN_SIZE, min(size, self._MINIMAP_MAX_SIZE))
        outer_rect = QRect(
            display_rect.right() - size - self._MINIMAP_MARGIN,
            display_rect.top() + self._MINIMAP_MARGIN,
            size,
            size,
        )
        self._minimap_rect = QRect(outer_rect)
        if outer_rect.width() <= 0 or outer_rect.height() <= 0:
            return

        painter.save()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(8, 12, 18, 210))
        painter.drawRoundedRect(outer_rect, 10, 10)

        title_rect = QRect(
            outer_rect.left() + 10, outer_rect.top() + 6, outer_rect.width() - 20, 18
        )
        painter.setPen(QPen(QColor("#cfd8dc"), 1))
        painter.drawText(title_rect, Qt.AlignLeft | Qt.AlignVCenter, "design")

        content_rect = QRect(
            outer_rect.left() + 8,
            outer_rect.top() + 26,
            outer_rect.width() - 16,
            outer_rect.height() - 34,
        )
        if content_rect.width() <= 0 or content_rect.height() <= 0:
            painter.restore()
            return

        background = self._design_background_for_size(content_rect.size())
        if background is not None:
            if self._design_document.file_backed:
                background_rect = self._minimap_design_rect_for_bounds(
                    content_rect,
                    self._design_document.bounds,
                )
                painter.drawPixmap(
                    background_rect,
                    background,
                    QRectF(background.rect()),
                )
            else:
                painter.drawPixmap(content_rect.topLeft(), background)

        static_overlay = self._minimap_static_overlay_for_size(content_rect.size())
        if static_overlay is not None:
            painter.drawPixmap(content_rect.topLeft(), static_overlay)
        self._draw_design_position(painter, content_rect)

        painter.setPen(QPen(QColor("#546e7a"), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(outer_rect, 10, 10)
        painter.restore()

    def _design_background_for_size(self, size: QSize) -> QPixmap | None:
        if self._design_document is None:
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
        fitted_rect = self._minimap_design_rect_for_bounds(
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

    def _minimap_static_overlay_for_size(self, size: QSize) -> QPixmap | None:
        if self._design_document is None:
            self._minimap_static_overlay = None
            self._minimap_static_overlay_key = None
            return None
        cache_key = self._minimap_static_overlay_cache_key(size)
        if (
            self._minimap_static_overlay_key == cache_key
            and self._minimap_static_overlay is not None
        ):
            return self._minimap_static_overlay

        image = QImage(size, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        rect = QRect(QPoint(0, 0), size)
        self._draw_design_route(painter, rect)
        self._draw_probe_route(painter, rect)
        self._draw_design_marks(painter, rect)
        painter.end()
        self._minimap_static_overlay = QPixmap.fromImage(image)
        self._minimap_static_overlay_key = cache_key
        return self._minimap_static_overlay

    def _minimap_static_overlay_cache_key(self, size: QSize) -> tuple[object, ...]:
        return (
            id(self._design_document),
            int(size.width()),
            int(size.height()),
            tuple((target.id, target.design_center) for target in self._design_targets),
            self._selected_target_id,
            self._probe_route_snapshot,
            self._selected_route_point_index,
            self._selected_design_point,
            tuple(self._source_design_marks),
            tuple(self._check_design_marks),
        )

    def _start_minimap_background_render(
        self,
        size: QSize,
        cache_key: tuple[object, ...],
    ) -> None:
        if self._design_document is None or self._design_document.file_backed:
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

        def render_background() -> None:
            started = perf_counter()
            try:
                image, point_count = self._render_minimap_background_image(
                    document,
                    render_size,
                )
            except Exception:
                logger.exception("MINIMAP RENDER failed")
                image = None
                point_count = 0
            elapsed_ms = (perf_counter() - started) * 1000.0
            logger.debug(
                "MINIMAP RENDER complete points=%d elapsed_ms=%.2f",
                point_count,
                elapsed_ms,
            )
            self._background_ready.emit(
                generation,
                cache_key,
                render_size,
                image,
            )

        threading.Thread(
            target=render_background,
            name="DesignMinimapRender",
            daemon=True,
        ).start()

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

    @classmethod
    def _render_minimap_background_image(
        cls,
        document: DesignDocument,
        size: QSize,
    ) -> tuple[QImage, int]:
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

    def _draw_design_route(self, painter: QPainter, rect: QRect) -> None:
        if not self._design_targets:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#4dd0e1"), 1.5))
        path = QPainterPath()
        start = self._map_design_point_to_rect(
            self._design_targets[0].design_center, rect
        )
        path.moveTo(start)
        for target in self._design_targets[1:]:
            path.lineTo(self._map_design_point_to_rect(target.design_center, rect))
        painter.drawPath(path)
        for target in self._design_targets:
            point = self._map_design_point_to_rect(target.design_center, rect)
            painter.setPen(QPen(QColor("#4dd0e1"), 1))
            painter.setBrush(QColor(77, 208, 225, 160))
            radius = 3.5
            if target.id == self._selected_target_id:
                painter.setPen(QPen(QColor("#ff7043"), 2))
                painter.setBrush(QColor(255, 112, 67, 180))
                radius = 5.0
            painter.drawEllipse(point, radius, radius)
        if self._selected_design_point is not None:
            painter.setPen(QPen(QColor("#ffd54f"), 2))
            painter.setBrush(QColor(255, 213, 79, 160))
            painter.drawEllipse(
                self._map_design_point_to_rect(self._selected_design_point, rect),
                4.0,
                4.0,
            )
        painter.restore()

    def _draw_probe_route(self, painter: QPainter, rect: QRect) -> None:
        route = self._probe_route
        if route is None or not route.points:
            return
        points = [point for point in route.points if point.enabled]
        if not points:
            return
        draw_details = len(points) <= self._PROBE_ROUTE_DETAIL_POINT_LIMIT
        draw_labels = len(points) <= self._PROBE_ROUTE_LABEL_POINT_LIMIT

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        center_path = QPainterPath()
        first_point = self._map_design_point_to_rect(points[0].camera_center, rect)
        center_path.moveTo(first_point)
        for route_point in points[1:]:
            center_path.lineTo(
                self._map_design_point_to_rect(route_point.camera_center, rect)
            )
        painter.setPen(QPen(QColor("#29b6f6"), 2.0))
        painter.drawPath(center_path)
        painter.setPen(QPen(QColor("#e1f5fe"), 1.4))
        painter.setBrush(QColor(225, 245, 254, 210))
        if draw_details:
            for start, end in zip(points, points[1:]):
                self._draw_route_arrowhead(
                    painter,
                    self._map_design_point_to_rect(start.camera_center, rect),
                    self._map_design_point_to_rect(end.camera_center, rect),
                )

        connector_pen = QPen(QColor(207, 216, 220, 120), 1.0)
        connector_pen.setStyle(Qt.DotLine)
        needle_colors = (QColor("#ffd54f"), QColor("#ec407a"))
        font = QFont()
        font.setPointSize(7)
        painter.setFont(font)

        for route_index, route_point in enumerate(route.points):
            if not route_point.enabled:
                continue
            center = self._map_design_point_to_rect(route_point.camera_center, rect)
            is_selected = route_index == self._selected_route_point_index
            painter.setPen(QPen(QColor("#ff7043" if is_selected else "#29b6f6"), 2.0))
            painter.setBrush(QColor(41, 182, 246, 170))
            radius = 5.2 if is_selected else 4.0
            painter.drawEllipse(center, radius, radius)

            if draw_labels or is_selected:
                label_rect = QRectF(center.x() + 5.0, center.y() - 15.0, 30.0, 13.0)
                painter.setPen(QPen(QColor(0, 0, 0, 180), 3))
                painter.drawText(
                    label_rect,
                    Qt.AlignLeft | Qt.AlignVCenter,
                    str(route_index + 1),
                )
                painter.setPen(QPen(QColor("#e3f2fd"), 1))
                painter.drawText(
                    label_rect,
                    Qt.AlignLeft | Qt.AlignVCenter,
                    str(route_index + 1),
                )

            if not draw_details and not is_selected:
                continue
            hits = route.needle_hits_for_point(route_point)
            for needle_index, (_offset, hit) in enumerate(hits[:2]):
                needle_point = self._map_design_point_to_rect(hit, rect)
                painter.setPen(connector_pen)
                painter.drawLine(center, needle_point)
                color = needle_colors[min(needle_index, len(needle_colors) - 1)]
                painter.setPen(QPen(color, 1.6))
                painter.setBrush(QColor(color.red(), color.green(), color.blue(), 120))
                if needle_index == 0:
                    painter.drawLine(
                        QPointF(needle_point.x() - 4.0, needle_point.y()),
                        QPointF(needle_point.x() + 4.0, needle_point.y()),
                    )
                    painter.drawLine(
                        QPointF(needle_point.x(), needle_point.y() - 4.0),
                        QPointF(needle_point.x(), needle_point.y() + 4.0),
                    )
                    painter.drawEllipse(needle_point, 3.0, 3.0)
                else:
                    painter.drawLine(
                        QPointF(needle_point.x() - 4.0, needle_point.y() - 4.0),
                        QPointF(needle_point.x() + 4.0, needle_point.y() + 4.0),
                    )
                    painter.drawLine(
                        QPointF(needle_point.x() - 4.0, needle_point.y() + 4.0),
                        QPointF(needle_point.x() + 4.0, needle_point.y() - 4.0),
                    )
                    painter.drawEllipse(needle_point, 3.0, 3.0)
        painter.restore()

    @staticmethod
    def _draw_route_arrowhead(
        painter: QPainter,
        start: QPointF,
        end: QPointF,
    ) -> None:
        dx = float(end.x() - start.x())
        dy = float(end.y() - start.y())
        length = math.hypot(dx, dy)
        if length <= 1e-6:
            return
        ux = dx / length
        uy = dy / length
        px = -uy
        py = ux
        arrow_len = 11.0
        arrow_width = 7.0
        if length < arrow_len * 2.0:
            arrow_len = max(5.0, length * 0.32)
            arrow_width = min(arrow_width, arrow_len * 0.75)
        tip_x = float(start.x() + dx * 0.58)
        tip_y = float(start.y() + dy * 0.58)
        base_x = tip_x - ux * arrow_len
        base_y = tip_y - uy * arrow_len
        notch_x = base_x + ux * arrow_len * 0.22
        notch_y = base_y + uy * arrow_len * 0.22
        polygon = QPolygonF(
            [
                QPointF(tip_x, tip_y),
                QPointF(
                    base_x + px * arrow_width * 0.5, base_y + py * arrow_width * 0.5
                ),
                QPointF(notch_x, notch_y),
                QPointF(
                    base_x - px * arrow_width * 0.5, base_y - py * arrow_width * 0.5
                ),
            ]
        )
        painter.drawPolygon(polygon)

    def _draw_design_marks(self, painter: QPainter, rect: QRect) -> None:
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        for point in self._source_design_marks:
            mapped = self._map_design_point_to_rect(point, rect)
            painter.setPen(QPen(QColor("#ffb300"), 2))
            painter.setBrush(QColor(255, 179, 0, 170))
            painter.drawEllipse(mapped, 4.0, 4.0)
        for point in self._check_design_marks:
            mapped = self._map_design_point_to_rect(point, rect)
            painter.setPen(QPen(QColor("#ab47bc"), 2))
            painter.drawLine(
                QPointF(mapped.x() - 4.0, mapped.y()),
                QPointF(mapped.x() + 4.0, mapped.y()),
            )
            painter.drawLine(
                QPointF(mapped.x(), mapped.y() - 4.0),
                QPointF(mapped.x(), mapped.y() + 4.0),
            )
        painter.restore()

    def _draw_design_position(self, painter: QPainter, rect: QRect) -> None:
        if self._current_design_position is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setClipRect(QRectF(rect))
        center, inside = self._map_design_point_to_rect_clamped(
            self._current_design_position, rect
        )
        if self._fov_design_size is not None:
            half_w = abs(float(self._fov_design_size[0])) * 0.5
            half_h = abs(float(self._fov_design_size[1])) * 0.5
            top_left = self._map_design_point_to_rect(
                (
                    self._current_design_position[0] - half_w,
                    self._current_design_position[1] + half_h,
                ),
                rect,
            )
            bottom_right = self._map_design_point_to_rect(
                (
                    self._current_design_position[0] + half_w,
                    self._current_design_position[1] - half_h,
                ),
                rect,
            )
            fov_rect = self._visible_minimap_fov_rect(
                QRectF(top_left, bottom_right),
                rect,
            )
            painter.setPen(QPen(QColor("#81c784"), 1.2))
            painter.setBrush(Qt.NoBrush)
            if (
                fov_rect.isValid()
                and fov_rect.width() > 0.0
                and fov_rect.height() > 0.0
            ):
                painter.drawRect(fov_rect)
        painter.setPen(QPen(QColor(0, 0, 0, 150), 3.6))
        painter.drawLine(
            QPointF(rect.left() + 4.0, center.y()),
            QPointF(rect.right() - 4.0, center.y()),
        )
        painter.drawLine(
            QPointF(center.x(), rect.top() + 4.0),
            QPointF(center.x(), rect.bottom() - 4.0),
        )
        painter.setPen(QPen(QColor("#81c784"), 1.6))
        painter.drawLine(
            QPointF(rect.left() + 4.0, center.y()),
            QPointF(rect.right() - 4.0, center.y()),
        )
        painter.drawLine(
            QPointF(center.x(), rect.top() + 4.0),
            QPointF(center.x(), rect.bottom() - 4.0),
        )
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(129, 199, 132, 90))
        painter.drawEllipse(center, 8.0, 8.0)
        painter.setBrush(QColor(232, 245, 233, 230))
        painter.drawEllipse(center, 2.8, 2.8)
        painter.setPen(QPen(QColor("#e8f5e9"), 1.6))
        painter.drawLine(
            QPointF(center.x() - 7.0, center.y()),
            QPointF(center.x() + 7.0, center.y()),
        )
        painter.drawLine(
            QPointF(center.x(), center.y() - 7.0),
            QPointF(center.x(), center.y() + 7.0),
        )
        if not inside:
            label_rect = QRectF(center.x() - 18.0, center.y() - 22.0, 36.0, 14.0)
            painter.setPen(QPen(QColor("#e8f5e9"), 1))
            painter.drawText(label_rect, Qt.AlignCenter, "OUT")
        painter.restore()

    def _map_design_point_to_rect(
        self, point: tuple[float, float], rect: QRect
    ) -> QPointF:
        assert self._design_document is not None
        return self._map_design_point_to_rect_for_bounds(
            point,
            rect,
            self._design_document.bounds,
        )

    @staticmethod
    def _map_design_point_to_rect_for_bounds(
        point: tuple[float, float],
        rect: QRect,
        bounds: tuple[float, float, float, float],
    ) -> QPointF:
        left, bottom, right, top = bounds
        width = max(right - left, 1e-9)
        height = max(top - bottom, 1e-9)
        fitted = MicroscopeMinimap._minimap_design_rect_for_bounds(rect, bounds)
        x_pos = fitted.left() + (point[0] - left) * fitted.width() / width
        y_pos = fitted.top() + (top - point[1]) * fitted.height() / height
        return QPointF(float(x_pos), float(y_pos))

    @staticmethod
    def _minimap_design_rect_for_bounds(
        rect: QRect,
        bounds: tuple[float, float, float, float],
    ) -> QRectF:
        # This six-pixel inset is shared by raster placement and coordinate
        # mapping; changing either side independently would shift click targets.
        left, bottom, right, top = bounds
        width = max(float(right) - float(left), 1e-9)
        height = max(float(top) - float(bottom), 1e-9)
        pad = 6.0
        usable_width = max(float(rect.width()) - 2.0 * pad, 1.0)
        usable_height = max(float(rect.height()) - 2.0 * pad, 1.0)
        scale = min(usable_width / width, usable_height / height)
        return QRectF(
            float(rect.left()) + (float(rect.width()) - width * scale) * 0.5,
            float(rect.top()) + (float(rect.height()) - height * scale) * 0.5,
            width * scale,
            height * scale,
        )

    def _map_rect_point_to_design(
        self, point: QPoint | QPointF, rect: QRect
    ) -> tuple[float, float]:
        assert self._design_document is not None
        left, bottom, right, top = self._design_document.bounds
        width = max(right - left, 1e-9)
        height = max(top - bottom, 1e-9)
        fitted = self._minimap_design_rect_for_bounds(
            rect,
            self._design_document.bounds,
        )
        x_value = left + (float(point.x()) - fitted.left()) * width / fitted.width()
        y_value = top - (float(point.y()) - fitted.top()) * height / fitted.height()
        x_value = min(max(x_value, left), right)
        y_value = min(max(y_value, bottom), top)
        return (float(x_value), float(y_value))

    def _map_design_point_to_rect_clamped(
        self, point: tuple[float, float], rect: QRect
    ) -> tuple[QPointF, bool]:
        mapped = self._map_design_point_to_rect(point, rect)
        left = float(rect.left() + 6)
        right = float(rect.right() - 6)
        top = float(rect.top() + 6)
        bottom = float(rect.bottom() - 6)
        clamped_x = min(max(mapped.x(), left), right)
        clamped_y = min(max(mapped.y(), top), bottom)
        inside = (
            abs(clamped_x - mapped.x()) < 1e-6 and abs(clamped_y - mapped.y()) < 1e-6
        )
        return QPointF(clamped_x, clamped_y), inside

    def _emit_pending_minimap_click(self) -> None:
        if (
            self._pending_minimap_display_rect is None
            or self._pending_minimap_click_point is None
        ):
            self._pending_minimap_click_point = None
            self._pending_minimap_display_rect = None
            return
        design_point = self.map_click(
            self._pending_minimap_click_point, self._pending_minimap_display_rect
        )
        self._pending_minimap_click_point = None
        self._pending_minimap_display_rect = None
        if design_point is not None:
            self.clicked.emit(design_point[0], design_point[1])

    def map_click(
        self, point: QPoint | QPointF, display_rect: QRect
    ) -> tuple[float, float] | None:
        """Map a click in the current minimap to design coordinates."""

        if self._design_document is None:
            return None
        outer_rect = self._outer_rect(display_rect)
        if not outer_rect.contains(
            point.toPoint() if isinstance(point, QPointF) else point
        ):
            return None
        content_rect = self._content_rect(outer_rect)
        if content_rect.width() <= 0 or content_rect.height() <= 0:
            return None
        return self._map_rect_point_to_design(point, content_rect)

    def contains(self, point: QPoint | QPointF, display_rect: QRect) -> bool:
        """Return whether a widget-space point is inside the minimap."""

        mapped = point.toPoint() if isinstance(point, QPointF) else point
        return self._design_document is not None and self._outer_rect(
            display_rect
        ).contains(mapped)

    def queue_click(self, point: QPoint, display_rect: QRect) -> None:
        """Delay a single click long enough to preserve double-click handling."""

        self._pending_minimap_click_point = QPoint(point)
        self._pending_minimap_display_rect = QRect(display_rect)
        self._minimap_click_timer.start(self.click_delay_ms())

    def cancel_click(self) -> None:
        """Cancel a queued single click after a double click."""

        self._minimap_click_timer.stop()
        self._pending_minimap_click_point = None
        self._pending_minimap_display_rect = None

    @classmethod
    def click_delay_ms(cls) -> int:
        return max(
            1,
            int(QApplication.doubleClickInterval())
            + int(cls._MINIMAP_CLICK_DELAY_PADDING_MS),
        )

    @staticmethod
    def _visible_minimap_fov_rect(fov_rect: QRectF, content_rect: QRect) -> QRectF:
        bounds = QRectF(content_rect).adjusted(1.0, 1.0, -1.0, -1.0)
        return fov_rect.normalized().intersected(bounds)

    @staticmethod
    def _layer_color(layer_key: tuple[int, int]) -> QColor:
        hue = int((layer_key[0] * 57 + layer_key[1] * 19) % 360)
        return QColor.fromHsv(hue, 120, 145, 180)

    @classmethod
    def _outer_rect(cls, display_rect: QRect) -> QRect:
        size = int(min(display_rect.width(), display_rect.height()) * 0.24)
        size = max(cls._MINIMAP_MIN_SIZE, min(size, cls._MINIMAP_MAX_SIZE))
        return QRect(
            display_rect.right() - size - cls._MINIMAP_MARGIN,
            display_rect.top() + cls._MINIMAP_MARGIN,
            size,
            size,
        )

    @staticmethod
    def _content_rect(outer_rect: QRect) -> QRect:
        return QRect(
            outer_rect.left() + 8,
            outer_rect.top() + 26,
            outer_rect.width() - 16,
            outer_rect.height() - 34,
        )

    def _route_snapshot(
        self, route: MeasurementRoute | None
    ) -> tuple[object, ...] | None:
        if route is None:
            self._probe_route_snapshot_token = None
            return None
        points = route.points
        token = (
            id(route),
            route.name,
            str(route.path) if route.path is not None else "",
            route.updated_at_utc,
            len(points),
            id(points[0]) if points else 0,
            id(points[-1]) if points else 0,
            tuple(
                (offset.id, offset.dx, offset.dy, offset.source)
                for offset in route.needle_offsets
            ),
        )
        if (
            token == self._probe_route_snapshot_token
            and self._probe_route_snapshot is not None
        ):
            return self._probe_route_snapshot
        self._probe_route_snapshot_token = token
        return (
            route.name,
            str(route.path) if route.path is not None else "",
            tuple(
                (offset.id, offset.dx, offset.dy, offset.source)
                for offset in route.needle_offsets
            ),
            tuple(
                (
                    point.id,
                    point.label,
                    point.camera_center,
                    point.enabled,
                )
                for point in route.points
            ),
        )


__all__ = ["MicroscopeMinimap", "MinimapBackgroundCacheKey"]
