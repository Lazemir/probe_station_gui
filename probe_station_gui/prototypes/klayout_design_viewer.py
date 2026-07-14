"""PROTOTYPE: evaluate embedded KLayout rendering and geometry snap.

One standalone PySide6 window, no production design imports, no persistence,
and no hardware access. Delete or rewrite this module after evaluation.
"""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
import json
import math
from pathlib import Path
import sys
import threading
import time
from typing import Any, Iterable

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal, QObject
from PySide6.QtGui import QColor, QCloseEvent, QImage, QMouseEvent, QPainter, QPen, QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


PROTOTYPE_TITLE = "KLayout render prototype"
RENDER_MARGIN_FRACTION = 0.25
RENDER_OVERSAMPLING = 2
MAX_RENDER_DIMENSION_PX = 4096
DEFAULT_SNAP_RADIUS_PX = 12
DRAG_THRESHOLD_PX = 4
MIN_VIEW_SPAN = 1e-9


@dataclass(frozen=True)
class ViewBox:
    left: float
    bottom: float
    right: float
    top: float

    @property
    def width(self) -> float:
        return max(MIN_VIEW_SPAN, self.right - self.left)

    @property
    def height(self) -> float:
        return max(MIN_VIEW_SPAN, self.top - self.bottom)

    @property
    def center(self) -> tuple[float, float]:
        return ((self.left + self.right) * 0.5, (self.bottom + self.top) * 0.5)

    def shifted(self, dx: float, dy: float) -> "ViewBox":
        return ViewBox(
            self.left + dx,
            self.bottom + dy,
            self.right + dx,
            self.top + dy,
        )

    def expanded(self, fraction: float) -> "ViewBox":
        dx = self.width * fraction
        dy = self.height * fraction
        return ViewBox(self.left - dx, self.bottom - dy, self.right + dx, self.top + dy)

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.left, self.bottom, self.right, self.top)


@dataclass(frozen=True)
class RenderRequest:
    generation: int
    render_box: tuple[float, float, float, float]
    width: int
    height: int
    visible_layers: frozenset[str]


@dataclass(frozen=True)
class SnapRequest:
    request_id: int
    kind: str
    x: float
    y: float
    radius: float
    visible_layers: frozenset[str]


class PrototypeSignals(QObject):
    render_loaded = Signal(object, object, str, float)
    snap_loaded = Signal(float)
    frame_ready = Signal(int, object, object, float)
    snap_ready = Signal(int, str, object, float, int)
    failed = Signal(str, str)


def _layer_key(source: Any) -> str:
    return str(source).split("@", 1)[0]


class RenderWorker:
    """Own a Qt-less KLayout LayoutView on one daemon thread."""

    def __init__(self, path: Path, signals: PrototypeSignals) -> None:
        self._path = path
        self._signals = signals
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._stop = threading.Event()
        self._latest: RenderRequest | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="klayout-prototype-render",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def submit(self, request: RenderRequest) -> None:
        with self._lock:
            self._latest = request
        self._event.set()

    def stop(self) -> None:
        self._stop.set()
        self._event.set()
        self._thread.join(timeout=1.0)

    def _take_latest(self) -> RenderRequest | None:
        with self._lock:
            request = self._latest
            self._latest = None
            self._event.clear()
            return request

    def _run(self) -> None:
        view = None
        try:
            import klayout.db as db
            import klayout.lay as lay

            started = time.perf_counter()
            view = lay.LayoutView()
            cellview_index = view.load_layout(str(self._path), False)
            view.max_hier()
            cellview = view.cellview(cellview_index)
            bounds = cellview.cell.dbbox()
            layers = tuple(
                (_layer_key(node.source), _layer_key(node.source))
                for node in view.each_layer()
            )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self._signals.render_loaded.emit(
                ViewBox(bounds.left, bounds.bottom, bounds.right, bounds.top),
                layers,
                cellview.cell_name,
                elapsed_ms,
            )

            while not self._stop.is_set():
                self._event.wait(0.2)
                if self._stop.is_set():
                    break
                request = self._take_latest()
                if request is None:
                    continue

                layer_iterator = view.begin_layers()
                while not layer_iterator.at_end():
                    properties = layer_iterator.current().dup()
                    properties.visible = _layer_key(properties.source) in request.visible_layers
                    view.set_layer_properties(layer_iterator, properties)
                    layer_iterator.next()

                started = time.perf_counter()
                pixels = view.get_pixels_with_options(
                    request.width,
                    request.height,
                    1,
                    RENDER_OVERSAMPLING,
                    0,
                    db.DBox(*request.render_box),
                )
                png_data = bytes(pixels.to_png_data())
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                self._signals.frame_ready.emit(
                    request.generation,
                    png_data,
                    request.render_box,
                    elapsed_ms,
                )
        except Exception as exc:  # pragma: no cover - prototype diagnostics
            self._signals.failed.emit("Render", f"{type(exc).__name__}: {exc}")
        finally:
            if view is not None:
                try:
                    view.stop_redraw()
                except Exception:
                    pass


class SnapWorker:
    """Own a second KLayout database so rendering cannot postpone snap."""

    def __init__(self, path: Path, signals: PrototypeSignals) -> None:
        self._path = path
        self._signals = signals
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._stop = threading.Event()
        self._latest_hover: SnapRequest | None = None
        self._clicks: deque[SnapRequest] = deque()
        self._thread = threading.Thread(
            target=self._run,
            name="klayout-prototype-snap",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def submit(self, request: SnapRequest) -> None:
        with self._lock:
            if request.kind == "click":
                self._clicks.append(request)
            else:
                self._latest_hover = request
        self._event.set()

    def stop(self) -> None:
        self._stop.set()
        self._event.set()
        self._thread.join(timeout=1.0)

    def _take_next(self) -> SnapRequest | None:
        with self._lock:
            if self._clicks:
                return self._clicks.popleft()
            if self._latest_hover is not None:
                request = self._latest_hover
                self._latest_hover = None
                return request
            self._event.clear()
            return None

    def _run(self) -> None:
        try:
            import klayout.db as db

            started = time.perf_counter()
            layout = db.Layout()
            layout.read(str(self._path))
            top_cells = list(layout.top_cells())
            if not top_cells:
                raise RuntimeError("The design has no top cell")
            top_cell = max(top_cells, key=lambda cell: cell.dbbox().area())
            layer_indexes = {
                str(info): layout.layer(info)
                for info in layout.layer_infos()
            }
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self._signals.snap_loaded.emit(elapsed_ms)

            while not self._stop.is_set():
                self._event.wait(0.2)
                if self._stop.is_set():
                    break
                while not self._stop.is_set():
                    request = self._take_next()
                    if request is None:
                        break
                    started = time.perf_counter()
                    result, shape_count = _snap_near(
                        db,
                        top_cell,
                        layer_indexes,
                        request,
                    )
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    self._signals.snap_ready.emit(
                        request.request_id,
                        request.kind,
                        result,
                        elapsed_ms,
                        shape_count,
                    )
        except Exception as exc:  # pragma: no cover - prototype diagnostics
            self._signals.failed.emit("Snap", f"{type(exc).__name__}: {exc}")


def _shape_contours(shape: Any, transform: Any, db: Any) -> Iterable[tuple[list[Any], bool]]:
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


def _distance_to_segment(
    x: float,
    y: float,
    start: Any,
    end: Any,
) -> tuple[float, float, float]:
    dx = end.x - start.x
    dy = end.y - start.y
    length_squared = dx * dx + dy * dy
    if length_squared <= 0.0:
        return (math.hypot(x - start.x, y - start.y), start.x, start.y)
    fraction = ((x - start.x) * dx + (y - start.y) * dy) / length_squared
    fraction = min(1.0, max(0.0, fraction))
    closest_x = start.x + fraction * dx
    closest_y = start.y + fraction * dy
    return (math.hypot(x - closest_x, y - closest_y), closest_x, closest_y)


def _snap_near(
    db: Any,
    top_cell: Any,
    layer_indexes: dict[str, int],
    request: SnapRequest,
) -> tuple[dict[str, Any], int]:
    best_distance = request.radius
    best: dict[str, Any] | None = None
    shape_count = 0
    search_box = db.DBox(
        request.x - request.radius,
        request.y - request.radius,
        request.x + request.radius,
        request.y + request.radius,
    )

    for layer_key in request.visible_layers:
        layer_index = layer_indexes.get(layer_key)
        if layer_index is None:
            continue
        iterator = top_cell.begin_shapes_rec_touching(layer_index, search_box)
        try:
            while not iterator.at_end():
                shape_count += 1
                shape = iterator.shape()
                transform = iterator.dtrans()
                for points, closed in _shape_contours(shape, transform, db):
                    if not points:
                        continue
                    for point in points:
                        distance = math.hypot(request.x - point.x, request.y - point.y)
                        if distance <= best_distance:
                            best_distance = distance
                            best = {
                                "mode": "vertex",
                                "x": point.x,
                                "y": point.y,
                                "distance": distance,
                                "layer": layer_key,
                            }
                    segment_count = len(points) if closed else max(0, len(points) - 1)
                    for index in range(segment_count):
                        start = points[index]
                        end = points[(index + 1) % len(points)]
                        distance, closest_x, closest_y = _distance_to_segment(
                            request.x,
                            request.y,
                            start,
                            end,
                        )
                        if distance < best_distance:
                            best_distance = distance
                            best = {
                                "mode": "segment",
                                "x": closest_x,
                                "y": closest_y,
                                "distance": distance,
                                "layer": layer_key,
                                "segment": (start.x, start.y, end.x, end.y),
                            }
                iterator.next()
        finally:
            del iterator

    if best is None:
        best = {
            "mode": "free",
            "x": request.x,
            "y": request.y,
            "distance": 0.0,
            "layer": "",
        }
    return best, shape_count


def _fit_aspect(box: ViewBox, pixel_width: int, pixel_height: int) -> ViewBox:
    if pixel_width <= 0 or pixel_height <= 0:
        return box
    target_aspect = pixel_width / pixel_height
    box_aspect = box.width / box.height
    center_x, center_y = box.center
    if box_aspect < target_aspect:
        half_width = box.height * target_aspect * 0.5
        return ViewBox(center_x - half_width, box.bottom, center_x + half_width, box.top)
    half_height = box.width / target_aspect * 0.5
    return ViewBox(box.left, center_y - half_height, box.right, center_y + half_height)


class DesignCanvas(QWidget):
    viewport_changed = Signal()
    hover_requested = Signal(float, float, float)
    click_requested = Signal(float, float, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._full_box: ViewBox | None = None
        self._view_box: ViewBox | None = None
        self._image = QImage()
        self._image_box: ViewBox | None = None
        self._hover: dict[str, Any] | None = None
        self._points: list[dict[str, Any]] = []
        self._snap_radius_px = DEFAULT_SNAP_RADIUS_PX
        self._press_position: QPointF | None = None
        self._press_box: ViewBox | None = None
        self._dragging = False

    @property
    def view_box(self) -> ViewBox | None:
        return self._view_box

    def set_snap_radius(self, radius_px: int) -> None:
        self._snap_radius_px = radius_px

    def set_design_bounds(self, bounds: ViewBox) -> None:
        self._full_box = bounds
        self.fit_design()

    def fit_design(self) -> None:
        if self._full_box is None:
            return
        padded = self._full_box.expanded(0.03)
        self._view_box = _fit_aspect(padded, self.width(), self.height())
        self._hover = None
        self.update()
        self.viewport_changed.emit()

    def set_frame(self, image: QImage, image_box: ViewBox) -> None:
        self._image = image
        self._image_box = image_box
        self.update()

    def set_hover(self, hover: dict[str, Any] | None) -> None:
        self._hover = hover
        self.update()

    def add_point(self, point: dict[str, Any]) -> None:
        self._points.append(point)
        self._hover = point
        self.update()

    def clear_points(self) -> None:
        self._points.clear()
        self.update()

    def design_radius(self, pixels: float) -> float:
        if self._view_box is None or self.width() <= 0 or self.height() <= 0:
            return 0.0
        return max(
            self._view_box.width * pixels / self.width(),
            self._view_box.height * pixels / self.height(),
        )

    def screen_to_design(self, position: QPointF) -> tuple[float, float] | None:
        if self._view_box is None or self.width() <= 0 or self.height() <= 0:
            return None
        x = self._view_box.left + position.x() * self._view_box.width / self.width()
        y = self._view_box.top - position.y() * self._view_box.height / self.height()
        return (x, y)

    def design_to_screen(self, x: float, y: float) -> QPointF | None:
        if self._view_box is None:
            return None
        return QPointF(
            (x - self._view_box.left) * self.width() / self._view_box.width,
            (self._view_box.top - y) * self.height() / self._view_box.height,
        )

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(18, 20, 24))
        if not self._image.isNull() and self._image_box is not None and self._view_box is not None:
            target = QRectF(
                (self._image_box.left - self._view_box.left)
                * self.width()
                / self._view_box.width,
                (self._view_box.top - self._image_box.top)
                * self.height()
                / self._view_box.height,
                self._image_box.width * self.width() / self._view_box.width,
                self._image_box.height * self.height() / self._view_box.height,
            )
            painter.drawImage(target, self._image)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._hover is not None:
            self._draw_target(painter, self._hover, QColor(255, 210, 64), 7.0)
        for index, point in enumerate(self._points, start=1):
            self._draw_target(painter, point, QColor(255, 86, 96), 5.0)
            position = self.design_to_screen(point["x"], point["y"])
            if position is not None:
                painter.setPen(QColor(255, 255, 255))
                painter.drawText(position + QPointF(8.0, -8.0), str(index))

    def _draw_target(
        self,
        painter: QPainter,
        target: dict[str, Any],
        color: QColor,
        radius: float,
    ) -> None:
        position = self.design_to_screen(target["x"], target["y"])
        if position is None:
            return
        painter.setPen(QPen(color, 2.0))
        painter.drawEllipse(position, radius, radius)
        painter.drawLine(position + QPointF(-radius - 3.0, 0.0), position + QPointF(radius + 3.0, 0.0))
        painter.drawLine(position + QPointF(0.0, -radius - 3.0), position + QPointF(0.0, radius + 3.0))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._view_box is not None:
            self._press_position = event.position()
            self._press_box = self._view_box
            self._dragging = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._press_position is not None and self._press_box is not None:
            delta = event.position() - self._press_position
            if not self._dragging and delta.manhattanLength() >= DRAG_THRESHOLD_PX:
                self._dragging = True
            if self._dragging:
                dx = -delta.x() * self._press_box.width / max(1, self.width())
                dy = delta.y() * self._press_box.height / max(1, self.height())
                self._view_box = self._press_box.shifted(dx, dy)
                self._hover = None
                self.update()
                self.viewport_changed.emit()
                event.accept()
                return
        point = self.screen_to_design(event.position())
        if point is not None:
            self.hover_requested.emit(
                point[0],
                point[1],
                self.design_radius(self._snap_radius_px),
            )
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._press_position is not None:
            was_dragging = self._dragging
            self._press_position = None
            self._press_box = None
            self._dragging = False
            if was_dragging:
                self.viewport_changed.emit()
            else:
                point = self.screen_to_design(event.position())
                if point is not None:
                    self.click_requested.emit(
                        point[0],
                        point[1],
                        self.design_radius(self._snap_radius_px),
                    )
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self._view_box is None:
            return
        anchor = self.screen_to_design(event.position())
        if anchor is None:
            return
        factor = 0.8 if event.angleDelta().y() > 0 else 1.25
        self._view_box = ViewBox(
            anchor[0] - (anchor[0] - self._view_box.left) * factor,
            anchor[1] - (anchor[1] - self._view_box.bottom) * factor,
            anchor[0] + (self._view_box.right - anchor[0]) * factor,
            anchor[1] + (self._view_box.top - anchor[1]) * factor,
        )
        self._hover = None
        self.update()
        self.viewport_changed.emit()
        event.accept()

    def leaveEvent(self, event: Any) -> None:
        self._hover = None
        self.update()
        super().leaveEvent(event)

    def resizeEvent(self, event: Any) -> None:
        if self._view_box is not None:
            self._view_box = _fit_aspect(self._view_box, self.width(), self.height())
            QTimer.singleShot(0, self.viewport_changed.emit)
        super().resizeEvent(event)


class PrototypeWindow(QMainWindow):
    def __init__(self, initial_path: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle(PROTOTYPE_TITLE)
        self.resize(1200, 820)

        self._signals = PrototypeSignals(self)
        self._signals.render_loaded.connect(self._on_render_loaded)
        self._signals.snap_loaded.connect(self._on_snap_loaded)
        self._signals.frame_ready.connect(self._on_frame_ready)
        self._signals.snap_ready.connect(self._on_snap_ready)
        self._signals.failed.connect(self._on_failed)
        self._render_worker: RenderWorker | None = None
        self._snap_worker: SnapWorker | None = None
        self._visible_layers: set[str] = set()
        self._render_generation = 0
        self._snap_request_id = 0
        self._latest_hover_request_id = 0
        self._render_ready = False
        self._snap_ready_state = False

        root = QWidget(self)
        layout = QVBoxLayout(root)
        toolbar = QHBoxLayout()
        self._open_button = QPushButton("Open GDS", root)
        self._fit_button = QPushButton("Fit", root)
        self._clear_button = QPushButton("Clear points", root)
        self._snap_radius = QSpinBox(root)
        self._snap_radius.setRange(2, 50)
        self._snap_radius.setValue(DEFAULT_SNAP_RADIUS_PX)
        self._layer_widget = QWidget(root)
        self._layer_layout = QHBoxLayout(self._layer_widget)
        self._layer_layout.setContentsMargins(0, 0, 0, 0)
        toolbar.addWidget(self._open_button)
        toolbar.addWidget(self._fit_button)
        toolbar.addWidget(self._clear_button)
        toolbar.addSpacing(10)
        toolbar.addWidget(QLabel("Snap px", root))
        toolbar.addWidget(self._snap_radius)
        toolbar.addSpacing(10)
        toolbar.addWidget(self._layer_widget)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self._canvas = DesignCanvas(root)
        layout.addWidget(self._canvas, 1)

        self._status = QLabel("Open a GDS design", root)
        self._status.setStyleSheet("QLabel { padding: 4px; color: #cfd8dc; }")
        layout.addWidget(self._status)
        self.setCentralWidget(root)

        self._open_button.clicked.connect(self._choose_file)
        self._fit_button.clicked.connect(self._canvas.fit_design)
        self._clear_button.clicked.connect(self._canvas.clear_points)
        self._snap_radius.valueChanged.connect(self._canvas.set_snap_radius)
        self._canvas.viewport_changed.connect(self._request_render)
        self._canvas.hover_requested.connect(self._request_hover_snap)
        self._canvas.click_requested.connect(self._request_click_snap)

        if initial_path is not None:
            QTimer.singleShot(0, lambda: self.load_design(initial_path))
        else:
            QTimer.singleShot(0, self._choose_file)

    def _choose_file(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Open GDS design",
            "",
            "GDS designs (*.gds *.gdsii);;All files (*)",
        )
        if filename:
            self.load_design(Path(filename))

    def load_design(self, path: Path) -> None:
        self._stop_workers()
        self._render_ready = False
        self._snap_ready_state = False
        self._visible_layers.clear()
        self._status.setText(f"Loading {path.name}")
        self.setWindowTitle(f"{PROTOTYPE_TITLE} — {path.name}")
        self._render_worker = RenderWorker(path, self._signals)
        self._snap_worker = SnapWorker(path, self._signals)
        self._render_worker.start()
        self._snap_worker.start()

    def _on_render_loaded(
        self,
        bounds: ViewBox,
        layers: tuple[tuple[str, str], ...],
        cell_name: str,
        elapsed_ms: float,
    ) -> None:
        self._render_ready = True
        self._visible_layers = {key for key, _label in layers}
        self._rebuild_layer_toggles(layers)
        self._status.setText(
            f"{cell_name} | Load {elapsed_ms:.1f} ms | rendering first frame"
        )
        self._canvas.set_design_bounds(bounds)

    def _on_snap_loaded(self, elapsed_ms: float) -> None:
        self._snap_ready_state = True
        self._status.setText(f"Snap ready in {elapsed_ms:.1f} ms")

    def _rebuild_layer_toggles(self, layers: tuple[tuple[str, str], ...]) -> None:
        while self._layer_layout.count():
            item = self._layer_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for layer_key, label in layers:
            checkbox = QCheckBox(f"Layer {label}", self._layer_widget)
            checkbox.setChecked(True)
            checkbox.toggled.connect(
                lambda checked, key=layer_key: self._set_layer_visible(key, checked)
            )
            self._layer_layout.addWidget(checkbox)

    def _set_layer_visible(self, layer_key: str, visible: bool) -> None:
        if visible:
            self._visible_layers.add(layer_key)
        else:
            self._visible_layers.discard(layer_key)
        self._canvas.set_hover(None)
        self._request_render()

    def _request_render(self) -> None:
        if not self._render_ready or self._render_worker is None:
            return
        view_box = self._canvas.view_box
        if view_box is None or self._canvas.width() <= 0 or self._canvas.height() <= 0:
            return
        render_box = view_box.expanded(RENDER_MARGIN_FRACTION)
        scale = 1.0 + 2.0 * RENDER_MARGIN_FRACTION
        device_scale = self._canvas.devicePixelRatioF()
        width = max(1, round(self._canvas.width() * scale * device_scale))
        height = max(1, round(self._canvas.height() * scale * device_scale))
        reduction = min(1.0, MAX_RENDER_DIMENSION_PX / max(width, height))
        width = max(1, round(width * reduction))
        height = max(1, round(height * reduction))
        self._render_generation += 1
        self._render_worker.submit(
            RenderRequest(
                generation=self._render_generation,
                render_box=render_box.as_tuple(),
                width=width,
                height=height,
                visible_layers=frozenset(self._visible_layers),
            )
        )

    def _on_frame_ready(
        self,
        generation: int,
        png_data: bytes,
        render_box_values: tuple[float, float, float, float],
        elapsed_ms: float,
    ) -> None:
        if generation != self._render_generation:
            return
        image = QImage.fromData(png_data)
        if image.isNull():
            self._on_failed("Render", "KLayout returned an invalid PNG frame")
            return
        self._canvas.set_frame(image, ViewBox(*render_box_values))
        self._status.setText(
            f"Render {elapsed_ms:.1f} ms | {image.width()}x{image.height()} | "
            f"snap {'ready' if self._snap_ready_state else 'loading'}"
        )

    def _request_hover_snap(self, x: float, y: float, radius: float) -> None:
        if not self._snap_ready_state or self._snap_worker is None:
            return
        self._snap_request_id += 1
        self._latest_hover_request_id = self._snap_request_id
        self._snap_worker.submit(
            SnapRequest(
                self._snap_request_id,
                "hover",
                x,
                y,
                radius,
                frozenset(self._visible_layers),
            )
        )

    def _request_click_snap(self, x: float, y: float, radius: float) -> None:
        if not self._snap_ready_state or self._snap_worker is None:
            self._status.setText("Snap is loading")
            return
        self._snap_request_id += 1
        self._snap_worker.submit(
            SnapRequest(
                self._snap_request_id,
                "click",
                x,
                y,
                radius,
                frozenset(self._visible_layers),
            )
        )

    def _on_snap_ready(
        self,
        request_id: int,
        kind: str,
        result: dict[str, Any],
        elapsed_ms: float,
        shape_count: int,
    ) -> None:
        if kind == "hover" and request_id != self._latest_hover_request_id:
            return
        if kind == "click":
            self._canvas.add_point(result)
        else:
            self._canvas.set_hover(result)
        layer_text = f" | layer {result['layer']}" if result.get("layer") else ""
        self._status.setText(
            f"Snap {result['mode']} {elapsed_ms:.2f} ms | "
            f"{shape_count} shapes{layer_text}"
        )

    def _on_failed(self, component: str, message: str) -> None:
        self._status.setText(f"{component}: {message}")
        QMessageBox.critical(self, PROTOTYPE_TITLE, f"{component}: {message}")

    def _stop_workers(self) -> None:
        if self._render_worker is not None:
            self._render_worker.stop()
            self._render_worker = None
        if self._snap_worker is not None:
            self._snap_worker.stop()
            self._snap_worker = None

    def closeEvent(self, event: QCloseEvent) -> None:
        self._stop_workers()
        super().closeEvent(event)


def _first_shape_point(db: Any, top_cell: Any, layer_index: int) -> tuple[float, float] | None:
    iterator = top_cell.begin_shapes_rec(layer_index)
    try:
        while not iterator.at_end():
            for points, _closed in _shape_contours(iterator.shape(), iterator.dtrans(), db):
                if points:
                    return (points[0].x, points[0].y)
            iterator.next()
    finally:
        del iterator
    return None


def _run_smoke(path: Path) -> int:
    import klayout.db as db
    import klayout.lay as lay

    started = time.perf_counter()
    view = lay.LayoutView()
    cellview_index = view.load_layout(str(path), False)
    view.max_hier()
    cellview = view.cellview(cellview_index)
    bounds = cellview.cell.dbbox()
    load_ms = (time.perf_counter() - started) * 1000.0

    started = time.perf_counter()
    pixels = view.get_pixels_with_options(800, 600, 1, 1, 0, bounds)
    png_data = bytes(pixels.to_png_data())
    render_ms = (time.perf_counter() - started) * 1000.0

    layout = db.Layout()
    layout.read(str(path))
    top_cell = max(layout.top_cells(), key=lambda cell: cell.dbbox().area())
    layer_indexes = {str(info): layout.layer(info) for info in layout.layer_infos()}
    visible_layers = frozenset(layer_indexes)
    first_point = None
    for layer_index in layer_indexes.values():
        first_point = _first_shape_point(db, top_cell, layer_index)
        if first_point is not None:
            break
    if first_point is None:
        raise RuntimeError("No snappable geometry found")
    radius = max(bounds.width(), bounds.height()) * 1e-6
    request = SnapRequest(1, "smoke", first_point[0], first_point[1], radius, visible_layers)
    started = time.perf_counter()
    snap, shape_count = _snap_near(db, top_cell, layer_indexes, request)
    snap_ms = (time.perf_counter() - started) * 1000.0
    view.stop_redraw()

    result = {
        "cell": cellview.cell_name,
        "layers": list(layer_indexes),
        "load_ms": round(load_ms, 3),
        "render_ms": round(render_ms, 3),
        "png_bytes": len(png_data),
        "snap_ms": round(snap_ms, 3),
        "snap_mode": snap["mode"],
        "snap_shapes": shape_count,
    }
    print(json.dumps(result, indent=2))
    return 0 if png_data and snap["mode"] != "free" else 1


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Throwaway embedded KLayout render and snap prototype",
    )
    parser.add_argument("design", nargs="?", type=Path, help="GDS design to open")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="render and query once without opening a Qt window",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.smoke:
        if args.design is None:
            raise SystemExit("--smoke requires a design path")
        return _run_smoke(args.design)

    app = QApplication.instance() or QApplication(sys.argv)
    window = PrototypeWindow(args.design)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
