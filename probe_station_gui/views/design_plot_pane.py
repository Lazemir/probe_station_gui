"""Pyqtgraph design plot pane for the design navigator."""

from __future__ import annotations

import logging
import math
import threading
from time import perf_counter, monotonic

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from probe_station_gui.design.navigation_geometry import (
    first_segment_length,
    route_arrow_segments,
    route_arrow_tip_fractions,
)
from probe_station_gui.design.model import (
    DesignDocument,
    LayerKey,
    MeasurementTarget,
    Point2D,
    SnapResult,
)
from probe_station_gui.design.klayout_types import (
    KLayoutConfig,
    PendingClick,
    SnapRequest,
    SnapResponse,
)
from probe_station_gui.design.klayout_workers import KLayoutSnapWorker
from probe_station_gui.route.model import MeasurementRoute
from probe_station_gui.views.design_klayout_raster import (
    KLayoutRasterController,
    KLayoutRasterItem,
)

try:  # pragma: no cover - optional runtime dependency
    import pyqtgraph as pg
except ImportError:  # pragma: no cover - optional runtime dependency
    pg = None


logger = logging.getLogger(__name__)

class _DesignPlotPane(QWidget):
    """Thin wrapper around pyqtgraph for design rendering."""

    calibration_point_selected = Signal(int, float, float)
    move_requested = Signal(float, float)
    route_point_requested = Signal(float, float)
    route_pick_requested = Signal(str, float, float)
    hover_snap_changed = Signal(object)
    snap_geometry_ready = Signal(int, object, object, object)
    HOVER_SNAP_LOG_INTERVAL_S = 1.0
    HOVER_SNAP_SLOW_MS = 8.0
    SNAP_RADIUS_PX = 14.0
    CURRENT_CROSSHAIR_HALF_SIZE_PX = 8.0
    PROBE_ROUTE_DETAIL_POINT_LIMIT = 300
    PROBE_ROUTE_LABEL_POINT_LIMIT = 150

    def __init__(self, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._document: DesignDocument | None = None
        self._targets: list[MeasurementTarget] = []
        self._selected_target_id: str | None = None
        self._probe_route: MeasurementRoute | None = None
        self._selected_route_point_index = -1
        self._probe_route_preview_points: list[Point2D] = []
        self._probe_route_preview_offsets: list[Point2D] = []
        self._tool_measure_segments: list[tuple[Point2D, Point2D]] = []
        self._tool_measure_points: list[Point2D] = []
        self._tool_measure_label_items: list[object] = []
        self._tool_sketch_segments: list[tuple[Point2D, Point2D]] = []
        self._tool_sketch_points: list[Point2D] = []
        self._source_design_marks: list[Point2D | None] = [None, None]
        self._current_design_position: Point2D | None = None
        self._fov_design_size: Point2D | None = None
        self._check_design_marks: list[Point2D] = []
        self._navigation_enabled = False
        self._route_edit_enabled = False
        self._route_pick_mode: str | None = None
        self._snap_enabled = True
        self._layer_items: list[object] = []
        self._hover_snap: SnapResult | None = None
        self._pending_hover_scene_pos = None
        self._last_hover_log_at = 0.0
        self._last_hover_log_signature: tuple[float, float, str] | None = None
        self._snap_generation = 0
        self._snap_request_id = 0
        self._latest_hover_request_id = 0
        self._pending_clicks: dict[int, PendingClick] = {}
        self._snap_worker: KLayoutSnapWorker | None = None
        self._snap_worker_path = None
        self._klayout_config: KLayoutConfig | None = None
        self._raster_item: KLayoutRasterItem | None = None
        self._raster_controller: KLayoutRasterController | None = None
        self._shutdown = False
        self._plot = None
        self._status_label: QLabel | None = None
        self._route_geometry_redraw_timer: QTimer | None = None
        self._route_geometry_deferred = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        if pg is None:
            status_label = QLabel("pyqtgraph is not installed.", self)
            status_label.setAlignment(Qt.AlignCenter)
            status_label.setMinimumHeight(320)
            status_label.setStyleSheet(
                "QLabel { border: 1px dashed palette(mid); color: palette(mid); }"
            )
            layout.addWidget(status_label, 1)
            return

        self._status_label = QLabel("No design loaded.", self)
        self._status_label.setAlignment(Qt.AlignCenter)
        self._status_label.setMinimumHeight(320)
        self._status_label.setStyleSheet(
            "QLabel { border: 1px dashed palette(mid); color: palette(mid); }"
        )
        self._plot = pg.PlotWidget(parent=self)
        self._plot.setBackground("k")
        self._plot.setMenuEnabled(False)
        self._plot.hideButtons()
        self._plot.setAspectLocked(True)
        self._plot.showGrid(x=False, y=False, alpha=0.0)
        view_box = self._plot.getViewBox()
        view_box.setMouseEnabled(x=True, y=True)
        self._plot.plotItem.hideAxis("bottom")
        self._plot.plotItem.hideAxis("left")

        self._raster_item = KLayoutRasterItem()
        self._plot.addItem(self._raster_item)
        self._raster_controller = KLayoutRasterController(
            view_box,
            self._raster_item,
            device_pixel_ratio=self._plot.devicePixelRatioF,
            parent=self,
        )
        self._raster_controller.failed.connect(self._on_klayout_render_failed)

        self._route_item = self._plot.plot([], [], pen=pg.mkPen("#4dd0e1", width=2))
        self._probe_route_item = self._plot.plot(
            [], [], pen=pg.mkPen("#29b6f6", width=2.5)
        )
        self._probe_route_arrow_item = self._plot.plot(
            [], [], pen=pg.mkPen("#64b5f6", width=1.35)
        )
        self._probe_needle_connector_item = self._plot.plot(
            [], [], pen=pg.mkPen("#cfd8dc", width=1, style=Qt.DotLine)
        )
        self._probe_route_preview_item = self._plot.plot(
            [], [], pen=pg.mkPen("#ffca28", width=2, style=Qt.DashLine)
        )
        self._probe_route_preview_arrow_item = self._plot.plot(
            [], [], pen=pg.mkPen("#64b5f6", width=1.35)
        )
        self._probe_preview_needle_connector_item = self._plot.plot(
            [], [], pen=pg.mkPen("#ffe082", width=1, style=Qt.DotLine)
        )
        self._probe_route_number_items: list[object] = []
        self._hover_segment_item = self._plot.plot(
            [],
            [],
            pen=pg.mkPen("#ffffff", width=2),
        )
        self._hover_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ffffff", width=2),
            brush=pg.mkBrush(255, 255, 255, 60),
            size=14,
            symbol="+",
        )
        self._target_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#4dd0e1", width=1),
            brush=pg.mkBrush(77, 208, 225, 150),
            size=8,
        )
        self._probe_route_point_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#29b6f6", width=1.5),
            brush=pg.mkBrush(41, 182, 246, 170),
            size=9,
            symbol="o",
        )
        self._probe_route_selected_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ff7043", width=2),
            brush=pg.mkBrush(255, 112, 67, 190),
            size=14,
            symbol="o",
        )
        self._probe_needle_1_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ffd54f", width=1.5),
            brush=pg.mkBrush(255, 213, 79, 180),
            size=10,
            symbol="+",
        )
        self._probe_needle_2_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ec407a", width=1.5),
            brush=pg.mkBrush(236, 64, 122, 170),
            size=10,
            symbol="x",
        )
        self._probe_route_preview_point_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ffca28", width=1.5),
            brush=pg.mkBrush(255, 202, 40, 100),
            size=8,
            symbol="o",
        )
        self._probe_preview_needle_1_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#fff59d", width=1.2),
            brush=pg.mkBrush(255, 245, 157, 130),
            size=8,
            symbol="+",
        )
        self._probe_preview_needle_2_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#f48fb1", width=1.2),
            brush=pg.mkBrush(244, 143, 177, 120),
            size=8,
            symbol="x",
        )
        self._tool_measure_item = self._plot.plot(
            [], [], pen=pg.mkPen("#ffffff", width=1.5, style=Qt.DashLine)
        )
        self._tool_measure_point_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ffffff", width=1.5),
            brush=pg.mkBrush(255, 255, 255, 80),
            size=8,
            symbol="o",
        )
        self._tool_sketch_item = self._plot.plot(
            [], [], pen=pg.mkPen("#90caf9", width=1.4, style=Qt.DashLine)
        )
        self._tool_sketch_point_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#90caf9", width=1.2),
            brush=pg.mkBrush(144, 202, 249, 110),
            size=7,
            symbol="o",
        )
        self._tool_sketch_midpoint_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ffffff", width=1.2),
            brush=pg.mkBrush(100, 181, 246, 150),
            size=8,
            symbol="+",
        )
        self._axis_triad_x_item = self._plot.plot(
            [], [], pen=pg.mkPen("#ef5350", width=2.2)
        )
        self._axis_triad_y_item = self._plot.plot(
            [], [], pen=pg.mkPen("#66bb6a", width=2.2)
        )
        self._axis_triad_z_item = self._plot.plot(
            [], [], pen=pg.mkPen("#42a5f5", width=2.2)
        )
        self._axis_triad_z_dot_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#42a5f5", width=1.0),
            brush=pg.mkBrush("#42a5f5"),
            size=5.0,
            symbol="o",
        )
        self._axis_triad_x_label = pg.TextItem(text="X", color="#ef5350", anchor=(0.0, 0.5))
        self._axis_triad_y_label = pg.TextItem(text="Y", color="#66bb6a", anchor=(0.5, 1.0))
        self._axis_triad_z_label = pg.TextItem(text="Z", color="#42a5f5", anchor=(1.0, 1.0))
        self._selected_target_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ff7043", width=2),
            brush=pg.mkBrush(255, 112, 67, 180),
            size=12,
        )
        self._current_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#81c784", width=1),
            brush=pg.mkBrush(129, 199, 132, 220),
            size=3.5,
            symbol="o",
        )
        self._current_crosshair_item = self._plot.plot(
            [],
            [],
            pen=pg.mkPen("#81c784", width=1.5),
        )
        self._source_mark_1_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ffd54f", width=2),
            brush=pg.mkBrush(255, 213, 79, 180),
            size=12,
            symbol="o",
        )
        self._source_mark_2_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ff7043", width=2),
            brush=pg.mkBrush(255, 112, 67, 180),
            size=12,
            symbol="t",
        )
        self._check_mark_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ab47bc", width=2),
            brush=pg.mkBrush(171, 71, 188, 180),
            size=9,
            symbol="x",
        )
        self._fov_item = self._plot.plot([], [], pen=pg.mkPen("#81c784", width=1))
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(16)
        self._hover_timer.timeout.connect(self._flush_hover_snap)
        self._route_geometry_redraw_timer = QTimer(self)
        self._route_geometry_redraw_timer.setSingleShot(True)
        self._route_geometry_redraw_timer.setInterval(0)
        self._route_geometry_redraw_timer.timeout.connect(self._redraw_route_geometry)
        self.snap_geometry_ready.connect(self._on_snap_geometry_ready)
        self._plot.addItem(self._hover_item)
        self._plot.addItem(self._target_item)
        self._plot.addItem(self._probe_route_point_item)
        self._plot.addItem(self._probe_route_selected_item)
        self._plot.addItem(self._probe_needle_1_item)
        self._plot.addItem(self._probe_needle_2_item)
        self._plot.addItem(self._probe_route_preview_point_item)
        self._plot.addItem(self._probe_preview_needle_1_item)
        self._plot.addItem(self._probe_preview_needle_2_item)
        self._plot.addItem(self._tool_measure_point_item)
        self._plot.addItem(self._tool_sketch_point_item)
        self._plot.addItem(self._tool_sketch_midpoint_item)
        self._plot.addItem(self._axis_triad_x_label)
        self._plot.addItem(self._axis_triad_y_label)
        self._plot.addItem(self._axis_triad_z_label)
        self._plot.addItem(self._axis_triad_z_dot_item)
        self._plot.addItem(self._selected_target_item)
        self._plot.addItem(self._current_item)
        self._plot.addItem(self._source_mark_1_item)
        self._plot.addItem(self._source_mark_2_item)
        self._plot.addItem(self._check_mark_item)
        self._plot.scene().sigMouseClicked.connect(self._on_mouse_clicked)
        self._plot.scene().sigMouseMoved.connect(self._on_mouse_moved)
        view_box.sigRangeChanged.connect(lambda *_unused: self._on_view_range_changed())
        layout.addWidget(self._status_label, 1)
        layout.addWidget(self._plot, 1)
        self._plot.hide()

    def _on_view_range_changed(self) -> None:
        self._schedule_route_geometry_redraw()
        self._redraw_axis_triad()
        self._redraw_current_position_overlay()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._schedule_route_geometry_redraw()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._schedule_route_geometry_redraw()

    def _schedule_route_geometry_redraw(self) -> None:
        if self._route_geometry_redraw_timer is None:
            return
        if self._route_geometry_deferred and self._route_geometry_redraw_timer.isActive():
            return
        self._route_geometry_deferred = True
        if not self._route_geometry_redraw_timer.isActive():
            self._route_geometry_redraw_timer.start()
        self._clear_route_arrows()

    def _redraw_route_geometry(self) -> None:
        if self._plot is None:
            return
        if self._probe_route is None and not self._probe_route_preview_points:
            self._route_geometry_deferred = False
            self._clear_route_arrows()
            return
        if self._route_geometry_pixel_size() is None:
            self._route_geometry_deferred = True
            self._clear_route_arrows()
            if self.isVisible() and self._route_geometry_redraw_timer is not None:
                self._route_geometry_redraw_timer.start(16)
            return
        self._route_geometry_deferred = False
        self._redraw_probe_route()
        self._redraw_route_preview()
        self._plot.update()

    def _clear_route_arrows(self) -> None:
        if self._plot is None:
            return
        self._probe_route_arrow_item.setData([], [])
        self._probe_route_preview_arrow_item.setData([], [])

    def set_document(self, document: DesignDocument | None) -> None:
        same_document = document is self._document
        self._document = document
        if document is None:
            self._snap_generation += 1
            self._latest_hover_request_id = 0
            self._pending_clicks.clear()
            self._set_hover_snap(None)
            self._detach_file_backed_document(timeout_s=0.5)
            self.set_status_message("No design loaded.")
            self._redraw_document()
        elif not same_document:
            self._snap_generation += 1
            self._latest_hover_request_id = 0
            self._pending_clicks.clear()
            self._set_hover_snap(None)
            self.set_status_message("")
            if document.file_backed:
                self._configure_file_backed_document(document)
            else:
                self._detach_file_backed_document(timeout_s=0.0)
            self._redraw_document()
            if not document.file_backed:
                self._start_snap_geometry_build(document)
        self._redraw_overlays()

    def _configure_file_backed_document(self, document: DesignDocument) -> None:
        controller = self._raster_controller
        if controller is None:
            return
        controller.set_document(document)
        config = controller.config
        self._klayout_config = config
        if config is None:
            return
        if self._snap_worker is not None and self._snap_worker_path == config.path:
            return
        self._stop_snap_worker(timeout_s=0.0)
        worker = KLayoutSnapWorker()
        worker.snap_ready.connect(self._on_file_backed_snap_ready)
        worker.failed.connect(self._on_klayout_snap_failed)
        self._snap_worker = worker
        self._snap_worker_path = config.path

    def _detach_file_backed_document(self, *, timeout_s: float) -> None:
        self._klayout_config = None
        self._latest_hover_request_id = 0
        self._pending_clicks.clear()
        self._stop_snap_worker(timeout_s=timeout_s)
        if self._raster_controller is not None:
            self._raster_controller.set_document(None)

    def _stop_snap_worker(self, *, timeout_s: float) -> None:
        worker = self._snap_worker
        self._snap_worker = None
        self._snap_worker_path = None
        if worker is None:
            return
        try:
            worker.snap_ready.disconnect(self._on_file_backed_snap_ready)
        except (RuntimeError, TypeError):
            pass
        try:
            worker.failed.disconnect(self._on_klayout_snap_failed)
        except (RuntimeError, TypeError):
            pass
        worker.stop(timeout_s=timeout_s)

    def _on_klayout_render_failed(self, message: str) -> None:
        logger.warning("KLayout design render failed: %s", message)
        self.set_status_message("Design rendering failed.")

    def _on_klayout_snap_failed(self, message: str) -> None:
        logger.warning("KLayout design snap failed: %s", message)

    def set_status_message(self, message: str) -> None:
        if self._status_label is None or self._plot is None:
            return
        message = str(message or "")
        if message:
            self._status_label.setText(message)
            self._status_label.show()
            self._plot.hide()
        else:
            self._status_label.hide()
            self._plot.show()

    def set_targets(
        self,
        targets: list[MeasurementTarget],
        *,
        selected_target_id: str | None,
    ) -> None:
        self._targets = list(targets)
        self._selected_target_id = selected_target_id
        self._redraw_overlays()

    def set_probe_route(
        self,
        route: MeasurementRoute | None,
        *,
        selected_route_point_index: int,
    ) -> None:
        self._probe_route = route
        self._selected_route_point_index = selected_route_point_index
        self._schedule_route_geometry_redraw()
        self._redraw_overlays()

    def set_probe_route_preview(self, preview: object) -> None:
        if (
            isinstance(preview, tuple)
            and len(preview) == 2
            and isinstance(preview[0], list)
            and isinstance(preview[1], list)
        ):
            self._probe_route_preview_points = [
                (float(point[0]), float(point[1]))
                for point in preview[0]
                if isinstance(point, (list, tuple)) and len(point) == 2
            ]
            self._probe_route_preview_offsets = [
                (float(offset[0]), float(offset[1]))
                for offset in preview[1]
                if isinstance(offset, (list, tuple)) and len(offset) == 2
            ][:2]
        else:
            self._probe_route_preview_points = []
            self._probe_route_preview_offsets = []
        self._schedule_route_geometry_redraw()
        self._redraw_overlays()

    def set_tool_measure_points(self, points: object) -> None:
        if isinstance(points, list):
            self._tool_measure_points = [
                (float(point[0]), float(point[1]))
                for point in points[:2]
                if isinstance(point, (list, tuple)) and len(point) == 2
            ]
        else:
            self._tool_measure_points = []
        self._redraw_overlays()

    def set_tool_measure_segments(self, segments: object) -> None:
        parsed: list[tuple[Point2D, Point2D]] = []
        if isinstance(segments, list):
            for item in segments:
                if (
                    isinstance(item, (list, tuple))
                    and len(item) == 2
                    and isinstance(item[0], (list, tuple))
                    and isinstance(item[1], (list, tuple))
                    and len(item[0]) == 2
                    and len(item[1]) == 2
                ):
                    parsed.append(
                        (
                            (float(item[0][0]), float(item[0][1])),
                            (float(item[1][0]), float(item[1][1])),
                        )
                    )
        self._tool_measure_segments = parsed
        self._redraw_overlays()

    def set_tool_sketch_points(self, points: object) -> None:
        if isinstance(points, list):
            self._tool_sketch_points = [
                (float(point[0]), float(point[1]))
                for point in points[:2]
                if isinstance(point, (list, tuple)) and len(point) == 2
            ]
        else:
            self._tool_sketch_points = []
        self._redraw_overlays()

    def set_tool_sketch_segments(self, segments: object) -> None:
        parsed: list[tuple[Point2D, Point2D]] = []
        if isinstance(segments, list):
            for item in segments:
                if (
                    isinstance(item, (list, tuple))
                    and len(item) == 2
                    and isinstance(item[0], (list, tuple))
                    and isinstance(item[1], (list, tuple))
                    and len(item[0]) == 2
                    and len(item[1]) == 2
                ):
                    parsed.append(
                        (
                            (float(item[0][0]), float(item[0][1])),
                            (float(item[1][0]), float(item[1][1])),
                        )
                    )
        self._tool_sketch_segments = parsed
        self._redraw_overlays()

    def set_source_design_marks(self, points: list[Point2D | None]) -> None:
        self._source_design_marks = list(points[:2])
        while len(self._source_design_marks) < 2:
            self._source_design_marks.append(None)
        self._redraw_overlays()

    def set_current_design_position(
        self,
        point: Point2D | None,
        *,
        fov_design_size: Point2D | None = None,
    ) -> None:
        self._current_design_position = point
        self._fov_design_size = fov_design_size
        self._redraw_current_position_overlay()

    def set_registration_marks(
        self,
        source_design_marks: list[Point2D | None],
        check_design_marks: list[Point2D],
    ) -> None:
        self._source_design_marks = list(source_design_marks[:2])
        while len(self._source_design_marks) < 2:
            self._source_design_marks.append(None)
        self._check_design_marks = list(check_design_marks)
        self._redraw_overlays()

    def set_navigation_enabled(self, enabled: bool) -> None:
        """Enable click-to-move on the layout plot once registration is valid."""

        self._navigation_enabled = bool(enabled)

    def set_route_edit_enabled(self, enabled: bool) -> None:
        """Enable route point placement by left-clicking the layout plot."""

        self._route_edit_enabled = bool(enabled)

    def set_route_pick_mode(self, mode: object) -> None:
        """Use the next left click as a route array helper point."""

        self._route_pick_mode = str(mode) if mode else None
        if self._plot is not None:
            self._plot.setCursor(
                Qt.CrossCursor if self._route_pick_mode else Qt.ArrowCursor
            )

    def set_snap_enabled(self, enabled: bool) -> None:
        """Enable or disable geometry snapping for design clicks and hover."""

        self._snap_enabled = bool(enabled)
        if not self._snap_enabled:
            self._set_hover_snap(None)

    def focus_bounds(self) -> None:
        if self._plot is None or self._document is None:
            return
        left, bottom, right, top = self._document.bounds
        self._plot.setXRange(left, right, padding=0.02)
        self._plot.setYRange(bottom, top, padding=0.02)

    def _redraw_document(self) -> None:
        if self._plot is None:
            return
        started = perf_counter()
        for item in self._layer_items:
            self._plot.removeItem(item)
        self._layer_items.clear()
        if self._document is None:
            return
        if self._document.file_backed:
            self.focus_bounds()
            self._schedule_route_geometry_redraw()
            return
        point_count = 0
        for layer_key, (x_data, y_data) in self._document.visible_plot_paths().items():
            if len(x_data) == 0:
                continue
            line = self._plot.plot(
                x_data,
                y_data,
                pen=pg.mkPen(self._layer_color(layer_key), width=1),
            )
            self._layer_items.append(line)
            point_count += len(x_data)
        self.focus_bounds()
        self._schedule_route_geometry_redraw()
        logger.debug(
            "DESIGN RENDER full items=%d points=%d elapsed_ms=%.2f",
            len(self._layer_items),
            point_count,
            (perf_counter() - started) * 1000.0,
        )

    def _start_snap_geometry_build(self, document: DesignDocument) -> None:
        if document.file_backed:
            return
        if document.has_snap_geometry():
            return
        generation = self._snap_generation

        def build_snap_geometry() -> None:
            started = perf_counter()
            try:
                geometry = document.build_snap_geometry()
            except Exception as exc:
                try:
                    self.snap_geometry_ready.emit(generation, document, None, exc)
                except RuntimeError:
                    pass
                return
            elapsed_ms = (perf_counter() - started) * 1000.0
            try:
                self.snap_geometry_ready.emit(generation, document, geometry, elapsed_ms)
            except RuntimeError:
                pass

        threading.Thread(
            target=build_snap_geometry,
            name="DesignSnapGeometryBuild",
            daemon=True,
        ).start()

    def _on_snap_geometry_ready(
        self,
        generation: int,
        document: object,
        geometry: object,
        result: object,
    ) -> None:
        if generation != self._snap_generation or document is not self._document:
            return
        if result is not None and not isinstance(result, (int, float)):
            logger.warning("Design snap geometry build failed: %s", result)
            return
        if not isinstance(document, DesignDocument) or not isinstance(geometry, tuple):
            return
        if len(geometry) < 3:
            return
        document.set_snap_geometry(*geometry)
        logger.debug(
            "DESIGN SNAP geometry ready vertices=%d segments=%d elapsed_ms=%.2f",
            len(document.snap_vertices),
            len(document.snap_segment_starts),
            float(result or 0.0),
        )
        if self._pending_hover_scene_pos is not None and not self._hover_timer.isActive():
            self._hover_timer.start()

    def _redraw_overlays(self) -> None:
        if self._plot is None:
            return
        if self._targets:
            x_values = [target.design_center[0] for target in self._targets]
            y_values = [target.design_center[1] for target in self._targets]
            self._target_item.setData(x_values, y_values)
            self._route_item.setData(x_values, y_values)
        else:
            self._target_item.setData([], [])
            self._route_item.setData([], [])

        self._redraw_probe_route()
        self._redraw_route_preview()
        self._redraw_tool_sketch()
        self._redraw_tool_measure()
        self._redraw_axis_triad()

        selected_target = next(
            (target for target in self._targets if target.id == self._selected_target_id),
            None,
        )
        if selected_target is None:
            self._selected_target_item.setData([], [])
        else:
            self._selected_target_item.setData(
                [selected_target.design_center[0]],
                [selected_target.design_center[1]],
            )

        self._redraw_current_position_overlay()

        self._set_slot_item_data(self._source_mark_1_item, self._source_design_marks[0])
        self._set_slot_item_data(self._source_mark_2_item, self._source_design_marks[1])
        if self._check_design_marks:
            self._check_mark_item.setData(
                [point[0] for point in self._check_design_marks],
                [point[1] for point in self._check_design_marks],
            )
        else:
            self._check_mark_item.setData([], [])
        self._redraw_hover()

    def _redraw_current_position_overlay(self) -> None:
        if self._plot is None:
            return
        if self._current_design_position is None:
            self._current_crosshair_item.setData([], [])
            self._current_item.setData([], [])
            self._fov_item.setData([], [])
            return

        pixel_size = self._data_units_per_screen_pixel()
        if self._document is None or pixel_size is None:
            self._current_crosshair_item.setData([], [])
        else:
            cx, cy = self._current_design_position
            half_size = self.CURRENT_CROSSHAIR_HALF_SIZE_PX * pixel_size
            self._current_crosshair_item.setData(
                [cx - half_size, cx + half_size, float("nan"), cx, cx],
                [cy, cy, float("nan"), cy - half_size, cy + half_size],
            )
        self._current_item.setData(
            [self._current_design_position[0]],
            [self._current_design_position[1]],
        )
        if self._fov_design_size is None:
            self._fov_item.setData([], [])
            return
        half_w = abs(float(self._fov_design_size[0])) * 0.5
        half_h = abs(float(self._fov_design_size[1])) * 0.5
        cx, cy = self._current_design_position
        xs = [cx - half_w, cx + half_w, cx + half_w, cx - half_w, cx - half_w]
        ys = [cy - half_h, cy - half_h, cy + half_h, cy + half_h, cy - half_h]
        self._fov_item.setData(xs, ys)

    def _redraw_probe_route(self) -> None:
        if self._plot is None:
            return
        if self._probe_route is None or not self._probe_route.points:
            self._probe_route_item.setData([], [])
            self._probe_route_arrow_item.setData([], [])
            self._probe_route_point_item.setData([], [])
            self._probe_route_selected_item.setData([], [])
            self._probe_needle_1_item.setData([], [])
            self._probe_needle_2_item.setData([], [])
            self._probe_needle_connector_item.setData([], [])
            self._clear_probe_route_numbers()
            return

        points = [point for point in self._probe_route.points if point.enabled]
        if not points:
            self._probe_route_item.setData([], [])
            self._probe_route_arrow_item.setData([], [])
            self._probe_route_point_item.setData([], [])
            self._probe_route_selected_item.setData([], [])
            self._probe_needle_1_item.setData([], [])
            self._probe_needle_2_item.setData([], [])
            self._probe_needle_connector_item.setData([], [])
            self._clear_probe_route_numbers()
            return

        centers_x = [point.camera_center[0] for point in points]
        centers_y = [point.camera_center[1] for point in points]
        self._probe_route_item.setData(centers_x, centers_y)
        self._probe_route_point_item.setData(centers_x, centers_y)
        self._probe_route_arrow_item.setData(
            *self._route_arrow_segments([point.camera_center for point in points])
        )
        self._redraw_probe_route_numbers()

        if 0 <= self._selected_route_point_index < len(self._probe_route.points):
            selected = self._probe_route.points[self._selected_route_point_index]
            self._probe_route_selected_item.setData(
                [selected.camera_center[0]],
                [selected.camera_center[1]],
            )
        else:
            self._probe_route_selected_item.setData([], [])

        draw_details = len(points) <= self.PROBE_ROUTE_DETAIL_POINT_LIMIT
        needle_1_x: list[float] = []
        needle_1_y: list[float] = []
        needle_2_x: list[float] = []
        needle_2_y: list[float] = []
        connector_x: list[float] = []
        connector_y: list[float] = []
        for route_index, point in enumerate(self._probe_route.points):
            if not point.enabled:
                continue
            if not draw_details and route_index != self._selected_route_point_index:
                continue
            center = point.camera_center
            hits = self._probe_route.needle_hits_for_point(point)
            for offset_index, (_offset, hit) in enumerate(hits[:2]):
                if offset_index == 0:
                    needle_1_x.append(hit[0])
                    needle_1_y.append(hit[1])
                else:
                    needle_2_x.append(hit[0])
                    needle_2_y.append(hit[1])
                connector_x.extend([center[0], hit[0], float("nan")])
                connector_y.extend([center[1], hit[1], float("nan")])
        self._probe_needle_1_item.setData(needle_1_x, needle_1_y)
        self._probe_needle_2_item.setData(needle_2_x, needle_2_y)
        self._probe_needle_connector_item.setData(connector_x, connector_y)

    def _redraw_route_preview(self) -> None:
        if self._plot is None:
            return
        points = list(self._probe_route_preview_points)
        if not points:
            self._probe_route_preview_item.setData([], [])
            self._probe_route_preview_arrow_item.setData([], [])
            self._probe_route_preview_point_item.setData([], [])
            self._probe_preview_needle_1_item.setData([], [])
            self._probe_preview_needle_2_item.setData([], [])
            self._probe_preview_needle_connector_item.setData([], [])
            return
        self._probe_route_preview_item.setData(
            [point[0] for point in points],
            [point[1] for point in points],
        )
        self._probe_route_preview_point_item.setData(
            [point[0] for point in points],
            [point[1] for point in points],
        )
        self._probe_route_preview_arrow_item.setData(
            *self._route_arrow_segments(points)
        )
        needle_1_x: list[float] = []
        needle_1_y: list[float] = []
        needle_2_x: list[float] = []
        needle_2_y: list[float] = []
        connector_x: list[float] = []
        connector_y: list[float] = []
        for center in points:
            for offset_index, offset in enumerate(self._probe_route_preview_offsets[:2]):
                hit = (center[0] + offset[0], center[1] + offset[1])
                if offset_index == 0:
                    needle_1_x.append(hit[0])
                    needle_1_y.append(hit[1])
                else:
                    needle_2_x.append(hit[0])
                    needle_2_y.append(hit[1])
                connector_x.extend([center[0], hit[0], float("nan")])
                connector_y.extend([center[1], hit[1], float("nan")])
        self._probe_preview_needle_1_item.setData(needle_1_x, needle_1_y)
        self._probe_preview_needle_2_item.setData(needle_2_x, needle_2_y)
        self._probe_preview_needle_connector_item.setData(connector_x, connector_y)

    def _redraw_tool_sketch(self) -> None:
        if self._plot is None:
            return
        segments = list(self._tool_sketch_segments)
        if len(self._tool_sketch_points) == 2:
            segments.append((self._tool_sketch_points[0], self._tool_sketch_points[1]))
        if not segments and not self._tool_sketch_points:
            self._tool_sketch_item.setData([], [])
            self._tool_sketch_point_item.setData([], [])
            self._tool_sketch_midpoint_item.setData([], [])
            return

        line_x: list[float] = []
        line_y: list[float] = []
        endpoint_points: list[Point2D] = []
        midpoint_points: list[Point2D] = []
        for start, end in segments:
            line_x.extend([start[0], end[0], float("nan")])
            line_y.extend([start[1], end[1], float("nan")])
            endpoint_points.extend([start, end])
            midpoint_points.append(
                (
                    (float(start[0]) + float(end[0])) * 0.5,
                    (float(start[1]) + float(end[1])) * 0.5,
                )
            )
        if len(self._tool_sketch_points) == 1:
            endpoint_points.append(self._tool_sketch_points[0])

        self._tool_sketch_item.setData(line_x, line_y)
        self._tool_sketch_point_item.setData(
            [point[0] for point in endpoint_points],
            [point[1] for point in endpoint_points],
        )
        self._tool_sketch_midpoint_item.setData(
            [point[0] for point in midpoint_points],
            [point[1] for point in midpoint_points],
        )

    def _redraw_tool_measure(self) -> None:
        if self._plot is None:
            return
        self._clear_tool_measure_labels()
        segments = list(self._tool_measure_segments)
        if len(self._tool_measure_points) == 2:
            segments.append((self._tool_measure_points[0], self._tool_measure_points[1]))
        if not segments and not self._tool_measure_points:
            self._tool_measure_item.setData([], [])
            self._tool_measure_point_item.setData([], [])
            return

        line_x: list[float] = []
        line_y: list[float] = []
        endpoint_points: list[Point2D] = []
        for start, end in segments:
            line_x.extend([start[0], end[0], float("nan")])
            line_y.extend([start[1], end[1], float("nan")])
            endpoint_points.extend([start, end])
            self._add_tool_measure_labels(start, end)
        if len(self._tool_measure_points) == 1:
            endpoint_points.append(self._tool_measure_points[0])

        self._tool_measure_item.setData(line_x, line_y)
        self._tool_measure_point_item.setData(
            [point[0] for point in endpoint_points],
            [point[1] for point in endpoint_points],
        )

    def _clear_tool_measure_labels(self) -> None:
        if self._plot is None:
            self._tool_measure_label_items.clear()
            return
        for item in self._tool_measure_label_items:
            self._plot.removeItem(item)
        self._tool_measure_label_items.clear()

    def _add_tool_measure_labels(self, start: Point2D, end: Point2D) -> None:
        if self._plot is None or pg is None:
            return
        dx = float(end[0]) - float(start[0])
        dy = float(end[1]) - float(start[1])
        length = math.hypot(dx, dy)
        if length <= 1e-12:
            return
        midpoint_x = (float(start[0]) + float(end[0])) * 0.5
        midpoint_y = (float(start[1]) + float(end[1])) * 0.5
        pixel_size = self._data_units_per_screen_pixel() or 1.0
        normal_x = -dy / length
        normal_y = dx / length
        angle = -math.degrees(math.atan2(dy, dx))
        label_specs = (
            (f"D={length:.3f}", "#fff59d", 18.0),
            (f"X={dx:.3f}", "#ef5350", 34.0),
            (f"Y={dy:.3f}", "#66bb6a", 50.0),
        )
        for text, color, offset_px in label_specs:
            item = pg.TextItem(
                text=text,
                color=color,
                anchor=(0.5, 1.0),
                fill=pg.mkBrush(0, 0, 0, 170),
            )
            try:
                item.setAngle(angle)
            except Exception:
                pass
            item.setPos(
                midpoint_x + normal_x * pixel_size * offset_px,
                midpoint_y + normal_y * pixel_size * offset_px,
            )
            self._plot.addItem(item)
            self._tool_measure_label_items.append(item)

    def _redraw_axis_triad(self) -> None:
        if self._plot is None:
            return
        if self._document is None:
            self._axis_triad_x_item.setData([], [])
            self._axis_triad_y_item.setData([], [])
            self._axis_triad_z_item.setData([], [])
            self._axis_triad_z_dot_item.setData([], [])
            for item in (
                self._axis_triad_x_label,
                self._axis_triad_y_label,
                self._axis_triad_z_label,
            ):
                item.setVisible(False)
            return
        pixel_size = self._data_units_per_screen_pixel()
        if pixel_size is None:
            return
        view_range = self._plot.getViewBox().viewRange()
        if not isinstance(view_range, list) or len(view_range) < 2:
            return
        x_min, _x_max = view_range[0]
        y_min, _y_max = view_range[1]
        margin = pixel_size * 34.0
        length = pixel_size * 42.0
        base = (float(x_min) + margin, float(y_min) + margin)
        x_end = (base[0] + length, base[1])
        y_end = (base[0], base[1] + length)
        arrow = pixel_size * 7.0
        z_radius = pixel_size * 7.0
        circle_points = [
            (
                base[0] + math.cos(index / 32.0 * math.tau) * z_radius,
                base[1] + math.sin(index / 32.0 * math.tau) * z_radius,
            )
            for index in range(33)
        ]
        self._axis_triad_x_item.setData(
            [base[0], x_end[0], float("nan"), x_end[0], x_end[0] - arrow, float("nan"), x_end[0], x_end[0] - arrow],
            [base[1], x_end[1], float("nan"), x_end[1], x_end[1] + arrow * 0.55, float("nan"), x_end[1], x_end[1] - arrow * 0.55],
        )
        self._axis_triad_y_item.setData(
            [base[0], y_end[0], float("nan"), y_end[0], y_end[0] - arrow * 0.55, float("nan"), y_end[0], y_end[0] + arrow * 0.55],
            [base[1], y_end[1], float("nan"), y_end[1], y_end[1] - arrow, float("nan"), y_end[1], y_end[1] - arrow],
        )
        self._axis_triad_z_item.setData(
            [point[0] for point in circle_points],
            [point[1] for point in circle_points],
        )
        self._axis_triad_z_dot_item.setData([base[0]], [base[1]])
        self._axis_triad_x_label.setPos(x_end[0] + pixel_size * 5.0, x_end[1])
        self._axis_triad_y_label.setPos(y_end[0], y_end[1] + pixel_size * 5.0)
        self._axis_triad_z_label.setPos(
            base[0] - pixel_size * 10.0,
            base[1] + pixel_size * 16.0,
        )
        for item in (
            self._axis_triad_x_label,
            self._axis_triad_y_label,
            self._axis_triad_z_label,
        ):
            item.setVisible(True)

    def _route_arrow_segments(
        self,
        centers: list[Point2D],
    ) -> tuple[list[float], list[float]]:
        pixel_size = self._route_geometry_pixel_size()
        return route_arrow_segments(
            centers,
            pixel_size=pixel_size,
            deferred=self._route_geometry_deferred,
        )

    @staticmethod
    def _first_segment_length(centers: list[Point2D]) -> float:
        return first_segment_length(centers)

    @staticmethod
    def _route_arrow_tip_fractions(
        segment_length: float,
        reference_length: float,
        arrow_len: float,
    ) -> list[float]:
        return route_arrow_tip_fractions(
            segment_length,
            reference_length,
            arrow_len,
        )

    def _clear_probe_route_numbers(self) -> None:
        if self._plot is None:
            self._probe_route_number_items.clear()
            return
        for item in self._probe_route_number_items:
            self._plot.removeItem(item)
        self._probe_route_number_items.clear()

    def _redraw_probe_route_numbers(self) -> None:
        if self._plot is None or pg is None:
            return
        self._clear_probe_route_numbers()
        if self._probe_route is None:
            return
        enabled_points = [point for point in self._probe_route.points if point.enabled]
        draw_labels = len(enabled_points) <= self.PROBE_ROUTE_LABEL_POINT_LIMIT
        for route_index, route_point in enumerate(self._probe_route.points):
            if not route_point.enabled:
                continue
            if not draw_labels and route_index != self._selected_route_point_index:
                continue
            item = pg.TextItem(
                text=str(route_index + 1),
                color="#e1f5fe",
                anchor=(0.0, 1.0),
                fill=pg.mkBrush(0, 0, 0, 130),
            )
            item.setPos(route_point.camera_center[0], route_point.camera_center[1])
            self._plot.addItem(item)
            self._probe_route_number_items.append(item)

    def _on_mouse_clicked(self, event) -> None:  # pragma: no cover - UI interaction
        if self._plot is None or self._document is None:
            return
        is_left_click = event.button() == Qt.LeftButton
        is_double_click = self._is_double_click_event(event)
        route_pick = self._route_pick_mode is not None and is_left_click
        route_click = (
            not route_pick
            and self._route_edit_enabled
            and is_left_click
        )
        if route_pick or route_click:
            slot = None
        elif self._navigation_enabled and is_left_click:
            if not is_double_click:
                return
            slot = None
        elif is_left_click:
            slot = 0
        elif event.button() == Qt.RightButton:
            slot = 1
        else:
            return
        if route_pick:
            action = "route_pick"
            payload: tuple[object, ...] = (str(self._route_pick_mode),)
        elif route_click:
            action = "route_point"
            payload = ()
        elif slot is None:
            action = "move"
            payload = ()
        else:
            action = "calibration"
            payload = (slot,)
        position = event.scenePos()
        if not self._plot.sceneBoundingRect().contains(position):
            return
        view_point = self._plot.getViewBox().mapSceneToView(position)
        raw_point = (float(view_point.x()), float(view_point.y()))
        if bool(getattr(self._document, "file_backed", False)):
            self._submit_file_backed_click(action, raw_point, payload)
            return
        started = perf_counter()
        snap_result = self._resolve_snap_result(raw_point)
        elapsed_ms = (perf_counter() - started) * 1000.0
        self._set_hover_snap(snap_result)
        logger.debug(
            "DESIGN SNAP click raw=(%.3f, %.3f) snapped=(%.3f, %.3f) mode=%s dist=%.4f elapsed_ms=%.2f",
            raw_point[0],
            raw_point[1],
            snap_result.point[0],
            snap_result.point[1],
            snap_result.mode,
            snap_result.distance,
            elapsed_ms,
        )
        if route_pick:
            self.route_pick_requested.emit(
                str(self._route_pick_mode),
                snap_result.point[0],
                snap_result.point[1],
            )
            return
        if route_click:
            self.route_point_requested.emit(
                snap_result.point[0],
                snap_result.point[1],
            )
            return
        if slot is None:
            self.move_requested.emit(
                snap_result.point[0],
                snap_result.point[1],
            )
            return
        self.calibration_point_selected.emit(
            slot,
            snap_result.point[0],
            snap_result.point[1],
        )

    def _submit_file_backed_click(
        self,
        action: str,
        raw_point: Point2D,
        payload: tuple[object, ...] = (),
    ) -> None:
        free_result = SnapResult(point=raw_point, mode="free", distance=0.0)
        if not self._snap_enabled:
            self._set_hover_snap(free_result)
            self._execute_click_action(action, payload, free_result)
            return
        config = self._klayout_config
        worker = self._snap_worker
        if config is None or worker is None:
            return
        snap_threshold = self._snap_distance_threshold()
        radius = 0.0 if snap_threshold is None else max(0.0, snap_threshold)
        self._snap_request_id += 1
        request_id = self._snap_request_id
        self._pending_clicks[request_id] = PendingClick(
            request_id=request_id,
            config_generation=config.generation,
            action=str(action),
            raw_point=raw_point,
            payload=tuple(payload),
        )
        worker.submit_click(
            SnapRequest(
                request_id=request_id,
                config=config,
                point=raw_point,
                radius=radius,
                purpose="click",
            )
        )

    def _execute_click_action(
        self,
        action: str,
        payload: tuple[object, ...],
        snap_result: SnapResult,
    ) -> None:
        x_value, y_value = snap_result.point
        if action == "route_pick" and payload:
            self.route_pick_requested.emit(str(payload[0]), x_value, y_value)
        elif action == "route_point":
            self.route_point_requested.emit(x_value, y_value)
        elif action == "move":
            self.move_requested.emit(x_value, y_value)
        elif action == "calibration" and payload:
            self.calibration_point_selected.emit(int(payload[0]), x_value, y_value)

    @staticmethod
    def _is_double_click_event(event) -> bool:
        double = getattr(event, "double", None)
        if not callable(double):
            return False
        try:
            return bool(double())
        except Exception:
            return False

    def _on_mouse_moved(self, position) -> None:  # pragma: no cover - UI interaction
        self._pending_hover_scene_pos = position
        if not self._hover_timer.isActive():
            self._hover_timer.start()

    def _flush_hover_snap(self) -> None:  # pragma: no cover - UI interaction
        if self._plot is None or self._document is None:
            self._set_hover_snap(None)
            return
        position = self._pending_hover_scene_pos
        self._pending_hover_scene_pos = None
        if position is None or not self._plot.sceneBoundingRect().contains(position):
            self._set_hover_snap(None)
            return
        view_point = self._plot.getViewBox().mapSceneToView(position)
        raw_point = (float(view_point.x()), float(view_point.y()))
        if self._document.file_backed:
            self._submit_file_backed_hover(raw_point)
            return
        started = perf_counter()
        snap_result = self._resolve_snap_result(raw_point)
        elapsed_ms = (perf_counter() - started) * 1000.0
        self._set_hover_snap(snap_result)
        self._log_hover_snap(raw_point, snap_result, elapsed_ms)

    def _submit_file_backed_hover(self, raw_point: Point2D) -> None:
        if not self._snap_enabled:
            self._set_hover_snap(None)
            return
        config = self._klayout_config
        worker = self._snap_worker
        snap_threshold = self._snap_distance_threshold()
        if config is None or worker is None or snap_threshold is None:
            self._set_hover_snap(None)
            return
        self._snap_request_id += 1
        self._latest_hover_request_id = self._snap_request_id
        worker.submit_hover(
            SnapRequest(
                request_id=self._snap_request_id,
                config=config,
                point=raw_point,
                radius=max(0.0, snap_threshold),
                purpose="hover",
            )
        )

    def _on_file_backed_snap_ready(self, response: SnapResponse) -> None:
        config = self._klayout_config
        if config is None or response.config_generation != config.generation:
            return
        if response.purpose == "hover":
            if response.request_id != self._latest_hover_request_id:
                return
            self._set_hover_snap(response.result)
            self._log_hover_snap(
                response.raw_point,
                response.result,
                response.elapsed_ms,
            )
            return
        if response.purpose != "click":
            return
        pending = self._pending_clicks.pop(response.request_id, None)
        if pending is None or pending.config_generation != config.generation:
            return
        self._set_hover_snap(response.result)
        logger.debug(
            "DESIGN SNAP click raw=(%.3f, %.3f) snapped=(%.3f, %.3f) mode=%s dist=%.4f elapsed_ms=%.2f",
            pending.raw_point[0],
            pending.raw_point[1],
            response.result.point[0],
            response.result.point[1],
            response.result.mode,
            response.result.distance,
            response.elapsed_ms,
        )
        self._execute_click_action(pending.action, pending.payload, response.result)

    def _resolve_snap_result(self, raw_point: Point2D) -> SnapResult:
        if self._document is None:
            return SnapResult(point=raw_point, mode="free", distance=0.0)
        if not self._snap_enabled:
            return SnapResult(point=raw_point, mode="free", distance=0.0)
        if self._document.file_backed:
            return SnapResult(point=raw_point, mode="free", distance=0.0)
        if not self._document.has_snap_geometry():
            return SnapResult(point=raw_point, mode="free", distance=0.0)
        snap_threshold = self._snap_distance_threshold()
        if snap_threshold is None:
            return SnapResult(point=raw_point, mode="free", distance=0.0)
        snap_result = self._document.snap_point_info(
            raw_point,
            max_distance=snap_threshold,
        )
        if snap_result.distance > snap_threshold:
            return SnapResult(point=raw_point, mode="free", distance=0.0)
        return snap_result

    def _route_geometry_pixel_size(self) -> float | None:
        if self._plot is None or not self._plot.isVisible():
            return None
        view_box = self._plot.getViewBox()
        scene_rect = view_box.sceneBoundingRect()
        if scene_rect.width() <= 0.0 or scene_rect.height() <= 0.0:
            return None
        pixel_size = self._data_units_per_screen_pixel()
        if pixel_size is None or not math.isfinite(pixel_size) or pixel_size <= 0.0:
            return None
        return pixel_size

    def _snap_distance_threshold(self) -> float | None:
        pixel_size = self._data_units_per_screen_pixel()
        if pixel_size is None or not math.isfinite(pixel_size) or pixel_size <= 0.0:
            return None
        return float(pixel_size * self.SNAP_RADIUS_PX)

    def _data_units_per_screen_pixel(self) -> float | None:
        if self._plot is None:
            return None
        view_box = self._plot.getViewBox()
        try:
            pixel_size = view_box.viewPixelSize()
        except Exception:
            pixel_size = None
        if (
            isinstance(pixel_size, tuple)
            and len(pixel_size) >= 2
            and all(math.isfinite(float(value)) and abs(float(value)) > 0.0 for value in pixel_size[:2])
        ):
            return max(abs(float(pixel_size[0])), abs(float(pixel_size[1])))
        view_range = view_box.viewRange()
        if not isinstance(view_range, list) or len(view_range) < 2:
            return None
        scene_rect = view_box.sceneBoundingRect()
        if scene_rect.width() <= 0.0 or scene_rect.height() <= 0.0:
            return None
        x_range = view_range[0]
        y_range = view_range[1]
        if len(x_range) < 2 or len(y_range) < 2:
            return None
        x_units = abs(float(x_range[1]) - float(x_range[0])) / float(scene_rect.width())
        y_units = abs(float(y_range[1]) - float(y_range[0])) / float(scene_rect.height())
        return max(x_units, y_units)

    def _set_hover_snap(self, snap_result: SnapResult | None) -> None:
        self._hover_snap = snap_result
        self._redraw_hover()
        self.hover_snap_changed.emit(snap_result)

    def _redraw_hover(self) -> None:
        if self._plot is None or self._hover_snap is None:
            if self._plot is not None:
                self._hover_item.setData([], [])
                self._hover_segment_item.setData([], [])
            return
        point = self._hover_snap.point
        self._hover_item.setData([point[0]], [point[1]])
        if (
            self._hover_snap.mode in {"segment", "segment_center"}
            and self._hover_snap.segment_start is not None
            and self._hover_snap.segment_end is not None
        ):
            self._hover_segment_item.setData(
                [self._hover_snap.segment_start[0], self._hover_snap.segment_end[0]],
                [self._hover_snap.segment_start[1], self._hover_snap.segment_end[1]],
            )
        else:
            self._hover_segment_item.setData([], [])

    def _log_hover_snap(
        self,
        raw_point: Point2D,
        snap_result: SnapResult,
        elapsed_ms: float,
    ) -> None:
        if not logger.isEnabledFor(logging.DEBUG):
            return
        signature = (
            round(snap_result.point[0], 3),
            round(snap_result.point[1], 3),
            snap_result.mode,
        )
        now = monotonic()
        should_log = (
            elapsed_ms >= self.HOVER_SNAP_SLOW_MS
            or (now - self._last_hover_log_at) >= self.HOVER_SNAP_LOG_INTERVAL_S
        )
        if not should_log:
            return
        logger.debug(
            "DESIGN SNAP hover raw=(%.3f, %.3f) snapped=(%.3f, %.3f) mode=%s dist=%.4f elapsed_ms=%.2f",
            raw_point[0],
            raw_point[1],
            snap_result.point[0],
            snap_result.point[1],
            snap_result.mode,
            snap_result.distance,
            elapsed_ms,
        )
        self._last_hover_log_at = now
        self._last_hover_log_signature = signature

    def shutdown(self) -> None:
        """Detach both KLayout workers from the Qt creator thread."""

        if self._shutdown:
            return
        self._shutdown = True
        self._pending_clicks.clear()
        self._latest_hover_request_id = 0
        if hasattr(self, "_hover_timer"):
            self._hover_timer.stop()
        if self._route_geometry_redraw_timer is not None:
            self._route_geometry_redraw_timer.stop()
        self._stop_snap_worker(timeout_s=0.5)
        self._klayout_config = None
        if self._raster_controller is not None:
            self._raster_controller.shutdown()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.shutdown()
        super().closeEvent(event)

    @staticmethod
    def _set_slot_item_data(item, point: Point2D | None) -> None:
        if point is None:
            item.setData([], [])
            return
        item.setData([point[0]], [point[1]])

    @staticmethod
    def _layer_color(layer_key: LayerKey) -> QColor:
        hue = int((layer_key[0] * 57 + layer_key[1] * 19) % 360)
        return QColor.fromHsv(hue, 180, 210, 180)


