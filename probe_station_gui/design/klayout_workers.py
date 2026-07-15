"""Independent, coalescing KLayout render and local-snap workers."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path
import threading
import time
from typing import Any, Protocol

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage, QTransform

from .klayout_geometry import Segment2D, select_snap
from .klayout_types import (
    KLayoutConfig,
    Point2D,
    RenderFrame,
    RenderRequest,
    SnapRequest,
    SnapResponse,
    forward_rotate_point,
    inverse_rotate_box,
    inverse_rotate_point,
)
from .model import SnapResult


class _RenderBackend(Protocol):
    def ensure_config(self, config: KLayoutConfig) -> None: ...

    def render(self, request: RenderRequest) -> RenderFrame: ...

    def close(self) -> None: ...


class _SnapBackend(Protocol):
    def ensure_config(self, config: KLayoutConfig) -> None: ...

    def snap(self, request: SnapRequest) -> SnapResponse: ...

    def close(self) -> None: ...


RenderBackendFactory = Callable[[], _RenderBackend]
SnapBackendFactory = Callable[[], _SnapBackend]


class KLayoutRenderWorker(QObject):
    """Render on one lazily-created daemon thread with a newest-only slot."""

    loaded = Signal(object)
    frame_ready = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        backend_factory: RenderBackendFactory | None = None,
    ) -> None:
        super().__init__(parent)
        self._backend_factory = backend_factory or _KLayoutRenderBackend
        self._condition = threading.Condition(threading.Lock())
        self._pending: RenderRequest | None = None
        self._latest_config: KLayoutConfig | None = None
        self._stopping = False
        self._thread: threading.Thread | None = None

    def submit(self, request: RenderRequest) -> None:
        with self._condition:
            if self._stopping:
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
        with self._condition:
            self._stopping = True
            self._pending = None
            thread = self._thread
            self._condition.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout_s)))

    def _run(self) -> None:
        backend: _RenderBackend | None = None
        try:
            backend = self._backend_factory()
            active_config: KLayoutConfig | None = None
            while True:
                request = self._take_pending()
                if request is None:
                    return
                try:
                    if active_config != request.config:
                        backend.ensure_config(request.config)
                        active_config = request.config
                        if self._may_emit(request.config):
                            self.loaded.emit(request.config)
                    frame = backend.render(request)
                except Exception as exc:
                    if self._may_emit(request.config):
                        self.failed.emit(f"{type(exc).__name__}: {exc}")
                    continue
                if self._may_emit(request.config):
                    self.frame_ready.emit(frame)
        except Exception as exc:
            if self._may_emit():
                self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            if backend is not None:
                try:
                    backend.close()
                except Exception as exc:
                    if self._may_emit():
                        self.failed.emit(f"{type(exc).__name__}: {exc}")

    def _take_pending(self) -> RenderRequest | None:
        with self._condition:
            while self._pending is None and not self._stopping:
                self._condition.wait()
            if self._stopping:
                return None
            request = self._pending
            self._pending = None
            return request

    def _may_emit(self, config: KLayoutConfig | None = None) -> bool:
        with self._condition:
            return not self._stopping and (
                config is None or config == self._latest_config
            )


class KLayoutSnapWorker(QObject):
    """Query a separate layout with click FIFO priority over newest hover."""

    loaded = Signal(object)
    snap_ready = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        backend_factory: SnapBackendFactory | None = None,
    ) -> None:
        super().__init__(parent)
        self._backend_factory = backend_factory or _KLayoutSnapBackend
        self._condition = threading.Condition(threading.Lock())
        self._hover: SnapRequest | None = None
        self._clicks: deque[SnapRequest] = deque()
        self._latest_config: KLayoutConfig | None = None
        self._stopping = False
        self._thread: threading.Thread | None = None

    def submit_hover(self, request: SnapRequest) -> None:
        self._submit(request, click=False)

    def submit_click(self, request: SnapRequest) -> None:
        self._submit(request, click=True)

    def stop(self, timeout_s: float = 1.0) -> None:
        with self._condition:
            self._stopping = True
            self._hover = None
            self._clicks.clear()
            thread = self._thread
            self._condition.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout_s)))

    def _submit(self, request: SnapRequest, *, click: bool) -> None:
        with self._condition:
            if self._stopping:
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
                request = self._take_next()
                if request is None:
                    return
                try:
                    if active_config != request.config:
                        backend.ensure_config(request.config)
                        active_config = request.config
                        if self._may_emit(request.config):
                            self.loaded.emit(request.config)
                    response = backend.snap(request)
                except Exception as exc:
                    if self._may_emit(request.config):
                        self.failed.emit(f"{type(exc).__name__}: {exc}")
                    continue
                if self._may_emit(request.config):
                    self.snap_ready.emit(response)
        except Exception as exc:
            if self._may_emit():
                self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            if backend is not None:
                try:
                    backend.close()
                except Exception as exc:
                    if self._may_emit():
                        self.failed.emit(f"{type(exc).__name__}: {exc}")

    def _take_next(self) -> SnapRequest | None:
        with self._condition:
            while not self._clicks and self._hover is None and not self._stopping:
                self._condition.wait()
            if self._stopping:
                return None
            while self._clicks:
                request = self._clicks.popleft()
                if request.config == self._latest_config:
                    return request
            request = self._hover
            self._hover = None
            if request is not None and request.config == self._latest_config:
                return request
            return None

    def _may_emit(self, config: KLayoutConfig | None = None) -> bool:
        with self._condition:
            return not self._stopping and (
                config is None or config == self._latest_config
            )


class _KLayoutRenderBackend:
    """Own the Qt-less KLayout view used by one render worker thread."""

    def __init__(self) -> None:
        self._db: Any = None
        self._view: Any = None
        self._cellview: Any = None
        self._cellview_index: int | None = None
        self._path: Path | None = None

    def ensure_config(self, config: KLayoutConfig) -> None:
        if self._path != config.path:
            self.close()
            import klayout.db as db
            import klayout.lay as lay

            self._db = db
            self._view = lay.LayoutView()
            self._cellview_index = self._view.load_layout(str(config.path), False)
            self._cellview = self._view.cellview(self._cellview_index)
            self._path = config.path

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

    def __init__(self) -> None:
        self._db: Any = None
        self._layout: Any = None
        self._top_cell: Any = None
        self._layer_indexes: dict[tuple[int, int], int] = {}
        self._path: Path | None = None

    def ensure_config(self, config: KLayoutConfig) -> None:
        if self._path != config.path:
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

        top_cell = self._layout.cell(config.top_cell_name)
        if top_cell is None:
            raise RuntimeError(f"Top cell '{config.top_cell_name}' does not exist")
        self._top_cell = top_cell

    def snap(self, request: SnapRequest) -> SnapResponse:
        started = time.perf_counter()
        source_point = inverse_rotate_point(request.point, request.config)
        search_box = self._db.DBox(
            source_point[0] - request.radius,
            source_point[1] - request.radius,
            source_point[0] + request.radius,
            source_point[1] + request.radius,
        )
        vertices: list[Point2D] = []
        segments: list[Segment2D] = []
        shapes_inspected = 0
        for layer_key in sorted(request.config.visible_layers):
            layer_index = self._layer_indexes.get(layer_key)
            if layer_index is None:
                continue
            iterator = self._top_cell.begin_shapes_rec_touching(
                layer_index,
                search_box,
            )
            try:
                while not iterator.at_end():
                    shapes_inspected += 1
                    shape = iterator.shape()
                    transform = iterator.dtrans()
                    for contour, closed in _shape_contours(shape, transform, self._db):
                        points = tuple((float(point.x), float(point.y)) for point in contour)
                        vertices.extend(points)
                        count = len(points) if closed else max(0, len(points) - 1)
                        segments.extend(
                            (points[index], points[(index + 1) % len(points)])
                            for index in range(count)
                        )
                    iterator.next()
            finally:
                del iterator

        source_result = select_snap(
            source_point,
            vertices,
            segments,
            request.radius,
        )
        result = _rotate_snap_result(source_result, request)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return SnapResponse(
            request_id=request.request_id,
            config_generation=request.config.generation,
            raw_point=request.point,
            result=result,
            elapsed_ms=elapsed_ms,
            shapes_inspected=shapes_inspected,
            purpose=request.purpose,
        )

    def close(self) -> None:
        top_cell = self._top_cell
        layout = self._layout
        self._top_cell = None
        self._layout = None
        self._layer_indexes = {}
        self._path = None
        self._db = None
        del top_cell
        del layout


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


def _shape_contours(
    shape: Any,
    transform: Any,
    db: Any,
) -> Iterable[tuple[list[Any], bool]]:
    if shape.is_box():
        box = shape.dbox
        points = [
            db.DPoint(box.left, box.bottom),
            db.DPoint(box.right, box.bottom),
            db.DPoint(box.right, box.top),
            db.DPoint(box.left, box.top),
        ]
        yield ([transform * point for point in points], True)
        return

    if shape.is_polygon():
        polygon = transform * shape.dpolygon
    elif shape.is_path():
        polygon = transform * shape.dpath.polygon()
    elif hasattr(shape, "is_edge") and shape.is_edge():
        edge = shape.dedge
        yield ([transform * edge.p1, transform * edge.p2], False)
        return
    else:
        return

    yield (list(polygon.each_point_hull()), True)
    for hole_index in range(polygon.holes()):
        yield (list(polygon.each_point_hole(hole_index)), True)


__all__ = ["KLayoutRenderWorker", "KLayoutSnapWorker"]
