"""Combined design window and controls for GDS-backed workflows."""

from __future__ import annotations

import math
import logging
import threading
from pathlib import Path
from time import perf_counter, monotonic

from PySide6.QtCore import QPointF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..design_model import (
    DesignDocument,
    LayerKey,
    MeasurementTarget,
    Point2D,
    SnapResult,
)
from ..route_model import MeasurementRoute

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
        self._plot = None
        self._status_label: QLabel | None = None

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
        self._redraw_axis_triad()
        self._redraw_current_position_overlay()

    def set_document(self, document: DesignDocument | None) -> None:
        same_document = document is self._document
        self._document = document
        if document is None:
            self._snap_generation += 1
            self._set_hover_snap(None)
            self.set_status_message("No design loaded.")
            self._redraw_document()
        elif not same_document:
            self._snap_generation += 1
            self._set_hover_snap(None)
            self.set_status_message("")
            self._redraw_document()
            self._start_snap_geometry_build(document)
        self._redraw_overlays()

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
        logger.debug(
            "DESIGN RENDER full items=%d points=%d elapsed_ms=%.2f",
            len(self._layer_items),
            point_count,
            (perf_counter() - started) * 1000.0,
        )

    def _start_snap_geometry_build(self, document: DesignDocument) -> None:
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

        needle_1_x: list[float] = []
        needle_1_y: list[float] = []
        needle_2_x: list[float] = []
        needle_2_y: list[float] = []
        connector_x: list[float] = []
        connector_y: list[float] = []
        for point in points:
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
        if len(centers) < 2:
            return [], []
        pixel_size = self._data_units_per_screen_pixel() or 1.0
        reference_length = self._first_segment_length(centers)
        if reference_length <= 1e-12:
            return [], []
        common_arrow_len = min(
            max(pixel_size * 5.0, reference_length * 0.18),
            pixel_size * 12.0,
        )
        common_arrow_width = common_arrow_len * 0.58
        x_values: list[float] = []
        y_values: list[float] = []
        for start, end in zip(centers, centers[1:]):
            dx = float(end[0] - start[0])
            dy = float(end[1] - start[1])
            length = math.hypot(dx, dy)
            if length <= 1e-12:
                continue
            ux = dx / length
            uy = dy / length
            px = -uy
            py = ux
            arrow_len = common_arrow_len
            arrow_width = common_arrow_width
            if length < arrow_len * 2.2:
                arrow_len = max(pixel_size * 5.0, length * 0.34)
                arrow_width = min(arrow_width, arrow_len * 0.7)
            for fraction in self._route_arrow_tip_fractions(
                length,
                reference_length,
                arrow_len,
            ):
                tip_x = float(start[0] + dx * fraction)
                tip_y = float(start[1] + dy * fraction)
                base_x = tip_x - ux * arrow_len
                base_y = tip_y - uy * arrow_len
                left_x = base_x + px * arrow_width * 0.5
                left_y = base_y + py * arrow_width * 0.5
                right_x = base_x - px * arrow_width * 0.5
                right_y = base_y - py * arrow_width * 0.5
                notch_x = base_x + ux * arrow_len * 0.22
                notch_y = base_y + uy * arrow_len * 0.22
                x_values.extend(
                    [
                        left_x,
                        tip_x,
                        right_x,
                        float("nan"),
                        left_x,
                        notch_x,
                        right_x,
                        float("nan"),
                    ]
                )
                y_values.extend(
                    [
                        left_y,
                        tip_y,
                        right_y,
                        float("nan"),
                        left_y,
                        notch_y,
                        right_y,
                        float("nan"),
                    ]
                )
        return x_values, y_values

    @staticmethod
    def _first_segment_length(centers: list[Point2D]) -> float:
        for start, end in zip(centers, centers[1:]):
            length = math.hypot(float(end[0] - start[0]), float(end[1] - start[1]))
            if length > 1e-12:
                return length
        return 0.0

    @staticmethod
    def _route_arrow_tip_fractions(
        segment_length: float,
        reference_length: float,
        arrow_len: float,
    ) -> list[float]:
        spacing = max(float(reference_length), float(arrow_len) * 4.0)
        if spacing <= 1e-12:
            return []
        arrow_count = max(1, min(80, int(round(float(segment_length) / spacing))))
        if arrow_count == 1:
            return [0.58]
        return [
            float(index + 1) / float(arrow_count + 1)
            for index in range(arrow_count)
        ]

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
        for route_index, route_point in enumerate(self._probe_route.points):
            if not route_point.enabled:
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
        route_pick = self._route_pick_mode is not None and event.button() == Qt.LeftButton
        route_click = (
            not route_pick
            and self._route_edit_enabled
            and event.button() == Qt.LeftButton
        )
        if route_pick or route_click:
            slot = None
        elif self._navigation_enabled and event.button() == Qt.LeftButton:
            slot = None
        elif event.button() == Qt.LeftButton:
            slot = 0
        elif event.button() == Qt.RightButton:
            slot = 1
        else:
            return
        position = event.scenePos()
        if not self._plot.sceneBoundingRect().contains(position):
            return
        view_point = self._plot.getViewBox().mapSceneToView(position)
        raw_point = (float(view_point.x()), float(view_point.y()))
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
        self.calibration_point_selected.emit(slot, snap_result.point[0], snap_result.point[1])

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
        started = perf_counter()
        snap_result = self._resolve_snap_result(raw_point)
        elapsed_ms = (perf_counter() - started) * 1000.0
        self._set_hover_snap(snap_result)
        self._log_hover_snap(raw_point, snap_result, elapsed_ms)

    def _resolve_snap_result(self, raw_point: Point2D) -> SnapResult:
        if self._document is None:
            return SnapResult(point=raw_point, mode="free", distance=0.0)
        if not self._snap_enabled:
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
            self._hover_snap.mode == "segment"
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


class DesignNavigatorPanel(QWidget):
    """Control panel for loading a design, registration, and target navigation."""

    load_design_requested = Signal(str)
    unload_design_requested = Signal()
    top_cell_changed = Signal(str)
    layer_visibility_changed = Signal(int, int, bool)
    design_rotate_requested = Signal(int)
    load_script_requested = Signal(str)
    reload_script_requested = Signal()
    move_to_target_requested = Signal(str)
    next_target_requested = Signal()
    previous_target_requested = Signal()
    target_selected = Signal(str)
    snap_enabled_changed = Signal(bool)
    route_new_requested = Signal()
    route_open_requested = Signal(str)
    route_save_requested = Signal()
    route_save_as_requested = Signal(str)
    route_add_current_requested = Signal()
    route_remove_selected_requested = Signal()
    route_clear_requested = Signal()
    route_selected = Signal(int)
    route_measurement_run_requested = Signal()
    route_measurement_stop_requested = Signal()
    route_measurement_interrupt_requested = Signal()
    route_measurement_save_shift_requested = Signal()
    route_measurement_confirmation_requested = Signal(str)
    route_measurement_jump_requested = Signal(int)
    route_offsets_changed = Signal(float, float, float, float)
    route_edit_enabled_changed = Signal(bool)
    route_pick_mode_changed = Signal(object)
    route_preview_changed = Signal(object)
    tool_measure_preview_changed = Signal(object)
    tool_measurements_changed = Signal(object)
    route_array_requested = Signal(
        float,
        float,
        float,
        float,
        int,
        float,
        float,
        int,
        bool,
        bool,
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._document: DesignDocument | None = None
        self._design_dialog_directory = ""
        self._snap_enabled = True
        self._source_design_marks: list[Point2D | None] = [None, None]
        self._source_stage_marks: list[Point2D | None] = [None, None]
        self._targets: list[MeasurementTarget] = []
        self._selected_target_id: str | None = None
        self._route: MeasurementRoute | None = None
        self._selected_route_point_index = -1
        self._route_measurement_running = False
        self._route_measurement_waiting = False
        self._design_registration_active = False
        self._current_design_position: Point2D | None = None
        self._active_design_tool = "select"
        self._route_pick_mode: str | None = None
        self._route_pick_anchor_mode: str | None = None
        self._route_pick_anchor_point: Point2D | None = None
        self._ruler_anchor: Point2D | None = None
        self._ruler_end: Point2D | None = None
        self._ruler_segments: list[tuple[Point2D, Point2D]] = []
        self._updating_route_controls = False

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.setSpacing(8)

        self._availability_label = QLabel(self)
        self._availability_label.setWordWrap(True)
        root_layout.addWidget(self._availability_label)
        self._snap_checkbox = QCheckBox("Snap To Geometry", self)
        self._snap_checkbox.setChecked(True)
        self._snap_checkbox.toggled.connect(self._on_snap_checkbox_toggled)
        root_layout.addWidget(self._snap_checkbox)
        self._snap_hint_label = QLabel("Hover snap: move over a line or corner.", self)
        self._snap_hint_label.setWordWrap(True)
        self._snap_hint_label.setStyleSheet("QLabel { color: #b0bec5; }")
        root_layout.addWidget(self._snap_hint_label)

        file_group = QGroupBox("Design", self)
        file_layout = QGridLayout(file_group)
        self._load_design_button = QPushButton("Load GDS...", file_group)
        self._unload_design_button = QPushButton("Unload", file_group)
        self._top_cell_combo = QComboBox(file_group)
        self._top_cell_combo.currentTextChanged.connect(self._on_top_cell_changed)
        self._load_design_button.clicked.connect(self._choose_design_file)
        self._unload_design_button.clicked.connect(self.unload_design_requested.emit)
        self._document_label = QLabel("No design loaded.", file_group)
        self._document_label.setWordWrap(True)
        file_layout.addWidget(self._load_design_button, 0, 0)
        file_layout.addWidget(self._unload_design_button, 0, 1)
        file_layout.addWidget(QLabel("Top cell:", file_group), 1, 0)
        file_layout.addWidget(self._top_cell_combo, 1, 1)
        file_layout.addWidget(self._document_label, 2, 0, 1, 2)
        root_layout.addWidget(file_group)

        layer_group = QGroupBox("Layers", self)
        layer_layout = QVBoxLayout(layer_group)
        self._layer_list = QListWidget(layer_group)
        self._layer_list.itemChanged.connect(self._on_layer_item_changed)
        layer_layout.addWidget(self._layer_list)
        root_layout.addWidget(layer_group)

        registration_group = QGroupBox("Registration", self)
        registration_layout = QVBoxLayout(registration_group)
        self._registration_hint_label = QLabel(
            "Use left click for design point 1 and right click for design point 2 in the layout view.",
            registration_group,
        )
        self._registration_hint_label.setWordWrap(True)
        registration_layout.addWidget(self._registration_hint_label)
        self._calibration_prompt_label = QLabel(registration_group)
        self._calibration_prompt_label.setWordWrap(True)
        registration_layout.addWidget(self._calibration_prompt_label)
        self._mark_1_label = QLabel(registration_group)
        self._mark_1_label.setWordWrap(True)
        self._mark_1_label.setStyleSheet("QLabel { font-weight: 600; }")
        registration_layout.addWidget(self._mark_1_label)
        self._mark_2_label = QLabel(registration_group)
        self._mark_2_label.setWordWrap(True)
        self._mark_2_label.setStyleSheet("QLabel { font-weight: 600; }")
        registration_layout.addWidget(self._mark_2_label)
        self._chip_1_label = QLabel(registration_group)
        self._chip_1_label.setWordWrap(True)
        self._chip_1_label.setStyleSheet("QLabel { font-weight: 600; }")
        registration_layout.addWidget(self._chip_1_label)
        self._chip_2_label = QLabel(registration_group)
        self._chip_2_label.setWordWrap(True)
        self._chip_2_label.setStyleSheet("QLabel { font-weight: 600; }")
        registration_layout.addWidget(self._chip_2_label)
        self._registration_status_label = QLabel("No design registration.", registration_group)
        self._registration_status_label.setWordWrap(True)
        registration_layout.addWidget(self._registration_status_label)
        root_layout.addWidget(registration_group)

        script_group = QGroupBox("Measurement Plan", self)
        script_layout = QVBoxLayout(script_group)
        script_buttons = QHBoxLayout()
        self._load_script_button = QPushButton("Load Script...", script_group)
        self._reload_script_button = QPushButton("Reload Script", script_group)
        script_buttons.addWidget(self._load_script_button)
        script_buttons.addWidget(self._reload_script_button)
        script_layout.addLayout(script_buttons)
        self._script_label = QLabel("No script loaded.", script_group)
        self._script_label.setWordWrap(True)
        script_layout.addWidget(self._script_label)
        self._target_table = QTableWidget(0, 4, script_group)
        self._target_table.setHorizontalHeaderLabels(["ID", "Label", "Group", "Design center"])
        self._target_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._target_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._target_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._target_table.itemSelectionChanged.connect(self._on_target_selection_changed)
        script_layout.addWidget(self._target_table)
        nav_buttons = QHBoxLayout()
        self._previous_button = QPushButton("Previous", script_group)
        self._move_button = QPushButton("Move To", script_group)
        self._next_button = QPushButton("Next", script_group)
        nav_buttons.addWidget(self._previous_button)
        nav_buttons.addWidget(self._move_button)
        nav_buttons.addWidget(self._next_button)
        script_layout.addLayout(nav_buttons)
        self._load_script_button.clicked.connect(self._choose_script_file)
        self._reload_script_button.clicked.connect(self.reload_script_requested.emit)
        self._previous_button.clicked.connect(self.previous_target_requested.emit)
        self._next_button.clicked.connect(self.next_target_requested.emit)
        self._move_button.clicked.connect(self._emit_move_to_selected_target)
        root_layout.addWidget(script_group)

        route_group = QGroupBox("Probe Route", self)
        route_layout = QVBoxLayout(route_group)
        self._route_layout = route_layout
        route_buttons = QHBoxLayout()
        self._route_new_button = QPushButton("New", route_group)
        self._route_open_button = QPushButton("Open...", route_group)
        self._route_save_button = QPushButton("Save", route_group)
        self._route_save_as_button = QPushButton("Save As...", route_group)
        route_buttons.addWidget(self._route_new_button)
        route_buttons.addWidget(self._route_open_button)
        route_buttons.addWidget(self._route_save_button)
        route_buttons.addWidget(self._route_save_as_button)
        route_layout.addLayout(route_buttons)
        self._route_label = QLabel("No route loaded.", route_group)
        self._route_label.setWordWrap(True)
        route_layout.addWidget(self._route_label)

        self._tool_toolbar_widget = QWidget(route_group)
        tool_buttons = QHBoxLayout(self._tool_toolbar_widget)
        tool_buttons.setContentsMargins(0, 0, 0, 0)
        tool_buttons.setSpacing(4)
        self._tool_button_group = QButtonGroup(self._tool_toolbar_widget)
        self._tool_button_group.setExclusive(True)
        self._select_tool_button = self._make_tool_button(
            self._tool_toolbar_widget,
            "Select",
            self._make_tool_icon("select"),
        )
        self._ruler_tool_button = self._make_tool_button(
            self._tool_toolbar_widget,
            "Measure",
            self._make_tool_icon("ruler"),
        )
        self._array_tool_button = self._make_tool_button(
            self._tool_toolbar_widget,
            "Array",
            self._make_tool_icon("array"),
        )
        self._rotate_tool_button = self._make_tool_button(
            self._tool_toolbar_widget,
            "Rotate",
            self._make_tool_icon("rotate"),
        )
        self._rotate_tool_button.setCheckable(False)
        self._tool_button_group.addButton(self._select_tool_button)
        self._tool_button_group.addButton(self._ruler_tool_button)
        self._tool_button_group.addButton(self._array_tool_button)
        self._select_tool_button.setChecked(True)
        tool_buttons.addWidget(self._select_tool_button)
        tool_buttons.addWidget(self._ruler_tool_button)
        tool_buttons.addWidget(self._array_tool_button)
        tool_buttons.addWidget(self._rotate_tool_button)
        tool_buttons.addStretch(1)
        route_layout.addWidget(self._tool_toolbar_widget)

        self._tool_group = QGroupBox("Tool Options", route_group)
        tool_layout = QVBoxLayout(self._tool_group)
        self._tool_status_label = QLabel("", self._tool_group)
        self._tool_status_label.setWordWrap(True)
        self._tool_status_label.setStyleSheet("QLabel { color: #607d8b; }")
        tool_layout.addWidget(self._tool_status_label)
        self._tool_stack = QStackedWidget(self._tool_group)

        select_page = QWidget(self._tool_group)
        select_layout = QVBoxLayout(select_page)
        select_layout.setContentsMargins(0, 0, 0, 0)
        select_layout.addWidget(QLabel("No tool active.", select_page))
        self._tool_stack.addWidget(select_page)

        ruler_page = QWidget(self._tool_group)
        ruler_layout = QGridLayout(ruler_page)
        ruler_layout.setContentsMargins(0, 0, 0, 0)
        self._ruler_start_label = QLabel("Start: not set", ruler_page)
        self._ruler_end_label = QLabel("End: not set", ruler_page)
        self._ruler_delta_label = QLabel("dX=0.000, dY=0.000", ruler_page)
        self._ruler_length_label = QLabel("Length=0.000, Angle=0.000 deg", ruler_page)
        self._ruler_clear_button = QPushButton("Clear", ruler_page)
        self._ruler_cancel_button = QPushButton("Cancel", ruler_page)
        ruler_layout.addWidget(self._ruler_start_label, 0, 0, 1, 2)
        ruler_layout.addWidget(self._ruler_end_label, 1, 0, 1, 2)
        ruler_layout.addWidget(self._ruler_delta_label, 2, 0, 1, 2)
        ruler_layout.addWidget(self._ruler_length_label, 3, 0, 1, 2)
        ruler_layout.addWidget(self._ruler_clear_button, 4, 0)
        ruler_layout.addWidget(self._ruler_cancel_button, 4, 1)
        self._tool_stack.addWidget(ruler_page)

        array_page = QWidget(self._tool_group)
        array_layout = QGridLayout(array_page)
        array_layout.setContentsMargins(0, 0, 0, 0)
        self._route_array_origin_x_spin = self._make_route_coordinate_spinbox(array_page)
        self._route_array_origin_y_spin = self._make_route_coordinate_spinbox(array_page)
        self._route_array_dir1_step_x_spin = self._make_route_distance_spinbox(array_page)
        self._route_array_dir1_step_y_spin = self._make_route_angle_spinbox(array_page)
        self._route_array_dir2_step_x_spin = self._make_route_distance_spinbox(array_page)
        self._route_array_dir2_step_y_spin = self._make_route_angle_spinbox(array_page)
        self._route_array_dir1_step_x_spin.setValue(100.0)
        self._route_array_dir2_step_x_spin.setValue(100.0)
        self._route_array_dir2_step_y_spin.setValue(90.0)
        self._route_array_dir1_count_spin = QSpinBox(array_page)
        self._route_array_dir1_count_spin.setRange(1, 10000)
        self._route_array_dir1_count_spin.setValue(8)
        self._route_array_dir2_count_spin = QSpinBox(array_page)
        self._route_array_dir2_count_spin.setRange(1, 10000)
        self._route_array_dir2_count_spin.setValue(1)
        self._route_array_serpentine_checkbox = QCheckBox("Serpentine", array_page)
        self._route_array_replace_checkbox = QCheckBox("Replace", array_page)
        self._route_array_pick_origin_button = self._make_icon_button(
            array_page, "Pick Origin", "origin"
        )
        self._route_array_pick_dir1_button = self._make_icon_button(
            array_page, "Pick Dir 1", "direction"
        )
        self._route_array_pick_extent1_button = self._make_icon_button(
            array_page, "Pick Extent 1", "extent"
        )
        self._route_array_pick_dir2_button = self._make_icon_button(
            array_page, "Pick Dir 2", "direction"
        )
        self._route_array_pick_extent2_button = self._make_icon_button(
            array_page, "Pick Extent 2", "extent"
        )
        self._route_array_create_button = self._make_icon_button(
            array_page, "Create", "accept"
        )
        self._route_array_cancel_button = self._make_icon_button(
            array_page, "Cancel", "cancel"
        )
        array_layout.addWidget(QLabel("Origin X", array_page), 0, 0)
        array_layout.addWidget(self._route_array_origin_x_spin, 0, 1)
        array_layout.addWidget(QLabel("Y", array_page), 0, 2)
        array_layout.addWidget(self._route_array_origin_y_spin, 0, 3)
        array_layout.addWidget(QLabel("Dir 1 length", array_page), 1, 0)
        array_layout.addWidget(self._route_array_dir1_step_x_spin, 1, 1)
        array_layout.addWidget(QLabel("angle", array_page), 1, 2)
        array_layout.addWidget(self._route_array_dir1_step_y_spin, 1, 3)
        array_layout.addWidget(QLabel("Count 1", array_page), 2, 0)
        array_layout.addWidget(self._route_array_dir1_count_spin, 2, 1)
        array_layout.addWidget(self._route_array_pick_dir1_button, 2, 2, 1, 2)
        array_layout.addWidget(QLabel("Dir 2 length", array_page), 3, 0)
        array_layout.addWidget(self._route_array_dir2_step_x_spin, 3, 1)
        array_layout.addWidget(QLabel("angle", array_page), 3, 2)
        array_layout.addWidget(self._route_array_dir2_step_y_spin, 3, 3)
        array_layout.addWidget(QLabel("Count 2", array_page), 4, 0)
        array_layout.addWidget(self._route_array_dir2_count_spin, 4, 1)
        array_layout.addWidget(self._route_array_pick_dir2_button, 4, 2, 1, 2)
        array_layout.addWidget(self._route_array_pick_origin_button, 5, 0, 1, 2)
        array_layout.addWidget(self._route_array_pick_extent1_button, 5, 2, 1, 2)
        array_layout.addWidget(self._route_array_pick_extent2_button, 6, 0, 1, 2)
        array_layout.addWidget(self._route_array_serpentine_checkbox, 6, 2, 1, 2)
        array_layout.addWidget(self._route_array_replace_checkbox, 7, 0, 1, 2)
        array_layout.addWidget(self._route_array_create_button, 7, 2)
        array_layout.addWidget(self._route_array_cancel_button, 7, 3)
        self._tool_stack.addWidget(array_page)

        tool_layout.addWidget(self._tool_stack)
        route_layout.addWidget(self._tool_group)

        offset_layout = QGridLayout()
        offset_layout.addWidget(QLabel("Needle", route_group), 0, 0)
        offset_layout.addWidget(QLabel("dx", route_group), 0, 1)
        offset_layout.addWidget(QLabel("dy", route_group), 0, 2)
        offset_layout.addWidget(QLabel("1", route_group), 1, 0)
        offset_layout.addWidget(QLabel("2", route_group), 2, 0)
        self._needle_1_dx_spin = self._make_route_offset_spinbox(route_group)
        self._needle_1_dy_spin = self._make_route_offset_spinbox(route_group)
        self._needle_2_dx_spin = self._make_route_offset_spinbox(route_group)
        self._needle_2_dy_spin = self._make_route_offset_spinbox(route_group)
        offset_layout.addWidget(self._needle_1_dx_spin, 1, 1)
        offset_layout.addWidget(self._needle_1_dy_spin, 1, 2)
        offset_layout.addWidget(self._needle_2_dx_spin, 2, 1)
        offset_layout.addWidget(self._needle_2_dy_spin, 2, 2)
        route_layout.addLayout(offset_layout)

        self._route_table = QTableWidget(0, 5, route_group)
        self._route_table.setHorizontalHeaderLabels(
            ["#", "Label", "Center", "N1", "N2"]
        )
        self._route_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._route_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._route_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._route_table.itemSelectionChanged.connect(
            self._on_route_selection_changed
        )
        route_layout.addWidget(self._route_table)

        route_edit_buttons = QHBoxLayout()
        self._route_add_current_button = QPushButton("Add Current", route_group)
        self._route_remove_button = QPushButton("Remove", route_group)
        self._route_clear_button = QPushButton("Clear", route_group)
        route_edit_buttons.addWidget(self._route_add_current_button)
        route_edit_buttons.addWidget(self._route_remove_button)
        route_edit_buttons.addWidget(self._route_clear_button)
        route_layout.addLayout(route_edit_buttons)

        route_run_buttons = QHBoxLayout()
        self._route_run_button = QPushButton("Run Route", route_group)
        self._route_interrupt_button = QPushButton("Interrupt", route_group)
        self._route_interrupt_button.setEnabled(False)
        self._route_stop_button = QPushButton("Cancel", route_group)
        self._route_stop_button.setEnabled(False)
        route_run_buttons.addWidget(self._route_run_button)
        route_run_buttons.addWidget(self._route_interrupt_button)
        route_run_buttons.addWidget(self._route_stop_button)
        route_layout.addLayout(route_run_buttons)
        route_confirm_buttons = QHBoxLayout()
        self._route_save_shift_button = QPushButton("Save Shift", route_group)
        self._route_save_shift_button.setEnabled(False)
        self._route_remeasure_button = QPushButton("Remeasure", route_group)
        self._route_remeasure_button.setEnabled(False)
        self._route_skip_button = QPushButton("Skip", route_group)
        self._route_skip_button.setEnabled(False)
        self._route_next_button = QPushButton("Next", route_group)
        self._route_next_button.setEnabled(False)
        self._route_jump_selected_button = QPushButton("Go Selected", route_group)
        self._route_jump_selected_button.setEnabled(False)
        route_confirm_buttons.addWidget(self._route_save_shift_button)
        route_confirm_buttons.addWidget(self._route_remeasure_button)
        route_confirm_buttons.addWidget(self._route_skip_button)
        route_confirm_buttons.addWidget(self._route_next_button)
        route_confirm_buttons.addWidget(self._route_jump_selected_button)
        route_layout.addLayout(route_confirm_buttons)
        self._route_run_status_label = QLabel("Route measurement idle.", route_group)
        self._route_run_status_label.setWordWrap(True)
        route_layout.addWidget(self._route_run_status_label)

        self._route_new_button.clicked.connect(self.route_new_requested.emit)
        self._route_open_button.clicked.connect(self._choose_route_file)
        self._route_save_button.clicked.connect(self.route_save_requested.emit)
        self._route_save_as_button.clicked.connect(self._choose_route_save_file)
        self._route_add_current_button.clicked.connect(
            self.route_add_current_requested.emit
        )
        self._route_remove_button.clicked.connect(
            self.route_remove_selected_requested.emit
        )
        self._route_clear_button.clicked.connect(self.route_clear_requested.emit)
        self._route_run_button.clicked.connect(
            self.route_measurement_run_requested.emit
        )
        self._route_stop_button.clicked.connect(
            self.route_measurement_stop_requested.emit
        )
        self._route_interrupt_button.clicked.connect(
            self.route_measurement_interrupt_requested.emit
        )
        self._route_save_shift_button.clicked.connect(
            self.route_measurement_save_shift_requested.emit
        )
        self._route_remeasure_button.clicked.connect(
            lambda _checked=False: self.route_measurement_confirmation_requested.emit(
                "remeasure"
            )
        )
        self._route_skip_button.clicked.connect(
            lambda _checked=False: self.route_measurement_confirmation_requested.emit(
                "skip"
            )
        )
        self._route_next_button.clicked.connect(
            lambda _checked=False: self.route_measurement_confirmation_requested.emit(
                "next"
            )
        )
        self._route_jump_selected_button.clicked.connect(
            self._emit_route_measurement_jump_to_selected
        )
        self._select_tool_button.clicked.connect(
            lambda _checked=False: self._set_design_tool("select")
        )
        self._ruler_tool_button.clicked.connect(
            lambda _checked=False: self._set_design_tool("ruler")
        )
        self._array_tool_button.clicked.connect(
            lambda _checked=False: self._set_design_tool("array")
        )
        self._rotate_tool_button.clicked.connect(
            lambda _checked=False: self.design_rotate_requested.emit(1)
        )
        self._ruler_clear_button.clicked.connect(self._clear_ruler)
        self._ruler_cancel_button.clicked.connect(
            lambda _checked=False: self._set_design_tool("select")
        )
        self._route_array_pick_origin_button.clicked.connect(
            lambda _checked=False: self._start_route_pick_mode("array_origin")
        )
        self._route_array_pick_dir1_button.clicked.connect(
            lambda _checked=False: self._start_route_pick_mode("array_dir1")
        )
        self._route_array_pick_extent1_button.clicked.connect(
            lambda _checked=False: self._start_route_pick_mode("array_extent1")
        )
        self._route_array_pick_dir2_button.clicked.connect(
            lambda _checked=False: self._start_route_pick_mode("array_dir2")
        )
        self._route_array_pick_extent2_button.clicked.connect(
            lambda _checked=False: self._start_route_pick_mode("array_extent2")
        )
        self._route_array_create_button.clicked.connect(self._emit_route_array_requested)
        self._route_array_cancel_button.clicked.connect(self._cancel_route_array)
        for widget in (
            self._route_array_origin_x_spin,
            self._route_array_origin_y_spin,
            self._route_array_dir1_step_x_spin,
            self._route_array_dir1_step_y_spin,
            self._route_array_dir1_count_spin,
            self._route_array_dir2_step_x_spin,
            self._route_array_dir2_step_y_spin,
            self._route_array_dir2_count_spin,
            self._route_array_serpentine_checkbox,
            self._route_array_replace_checkbox,
        ):
            if hasattr(widget, "valueChanged"):
                widget.valueChanged.connect(self._update_route_array_preview)
            else:
                widget.toggled.connect(self._update_route_array_preview)
        for spinbox in (
            self._needle_1_dx_spin,
            self._needle_1_dy_spin,
            self._needle_2_dx_spin,
            self._needle_2_dy_spin,
        ):
            spinbox.valueChanged.connect(self._emit_route_offsets_changed)
        root_layout.addWidget(route_group)

        self._current_position_label = QLabel("Stage: unavailable", self)
        self._current_position_label.setWordWrap(True)
        root_layout.addWidget(self._current_position_label)
        root_layout.addStretch(1)

        self._update_availability()
        self._update_enabled_state()
        self._set_design_tool("select")

    def set_document(self, document: DesignDocument | None) -> None:
        if document is self._document:
            return
        self._document = document
        self._source_design_marks = [None, None]
        self._source_stage_marks = [None, None]
        if document is None:
            self._document_label.setText("No design loaded.")
            self._top_cell_combo.blockSignals(True)
            self._top_cell_combo.clear()
            self._top_cell_combo.blockSignals(False)
            self._layer_list.blockSignals(True)
            self._layer_list.clear()
            self._layer_list.blockSignals(False)
        else:
            self._document_label.setText(
                f"{document.path.name} | bounds {self._format_bounds(document.bounds)}"
            )
            self._top_cell_combo.blockSignals(True)
            self._top_cell_combo.clear()
            self._top_cell_combo.addItems(document.cell_names)
            self._top_cell_combo.setCurrentText(document.top_cell_name)
            self._top_cell_combo.blockSignals(False)
            self._layer_list.blockSignals(True)
            self._layer_list.clear()
            for layer_key in document.layer_keys():
                item = QListWidgetItem(f"Layer {layer_key[0]}/{layer_key[1]}")
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setData(Qt.UserRole, layer_key)
                item.setCheckState(
                    Qt.Checked if layer_key in document.visible_layers else Qt.Unchecked
                )
                self._layer_list.addItem(item)
            self._layer_list.blockSignals(False)
        self._update_mark_labels()
        self._update_enabled_state()

    def set_script_path(self, script_path: str | None) -> None:
        if not script_path:
            self._script_label.setText("No script loaded.")
            return
        self._script_label.setText(str(Path(script_path)))

    def set_targets(
        self,
        targets: list[MeasurementTarget],
        *,
        selected_target_id: str | None,
    ) -> None:
        self._targets = list(targets)
        self._selected_target_id = selected_target_id
        self._target_table.blockSignals(True)
        self._target_table.clearSelection()
        self._target_table.setRowCount(len(targets))
        for row, target in enumerate(targets):
            self._target_table.setItem(row, 0, QTableWidgetItem(target.id))
            self._target_table.setItem(row, 1, QTableWidgetItem(target.label))
            self._target_table.setItem(row, 2, QTableWidgetItem(target.group or ""))
            self._target_table.setItem(
                row,
                3,
                QTableWidgetItem(
                    f"X={target.design_center[0]:.3f}, Y={target.design_center[1]:.3f}"
                ),
            )
            if target.id == selected_target_id:
                self._target_table.selectRow(row)
        self._target_table.blockSignals(False)
        self._update_enabled_state()

    def set_route(
        self,
        route: MeasurementRoute | None,
        *,
        selected_route_point_index: int,
    ) -> None:
        self._route = route
        self._selected_route_point_index = selected_route_point_index
        self._updating_route_controls = True
        try:
            self._route_table.blockSignals(True)
            self._route_table.clearSelection()
            if route is None:
                self._route_label.setText("No route loaded.")
                self._route_table.setRowCount(0)
                self._set_route_offset_values(0.0, 0.0, 0.0, 0.0)
            else:
                path_text = str(route.path) if route.path is not None else "Unsaved route."
                self._route_label.setText(f"{route.name} | {path_text}")
                self._route_table.setRowCount(len(route.points))
                for row, point in enumerate(route.points):
                    hits = route.needle_hits_for_point(point)
                    needle_1 = hits[0][1] if len(hits) > 0 else point.camera_center
                    needle_2 = hits[1][1] if len(hits) > 1 else point.camera_center
                    self._route_table.setItem(row, 0, QTableWidgetItem(str(row + 1)))
                    self._route_table.setItem(row, 1, QTableWidgetItem(point.label))
                    self._route_table.setItem(
                        row,
                        2,
                        QTableWidgetItem(self._format_point(point.camera_center)),
                    )
                    self._route_table.setItem(
                        row,
                        3,
                        QTableWidgetItem(self._format_point(needle_1)),
                    )
                    self._route_table.setItem(
                        row,
                        4,
                        QTableWidgetItem(self._format_point(needle_2)),
                    )
                    if row == selected_route_point_index:
                        self._route_table.selectRow(row)
                offsets = route.needle_offsets[:2]
                if len(offsets) >= 2:
                    self._set_route_offset_values(
                        offsets[0].dx,
                        offsets[0].dy,
                        offsets[1].dx,
                        offsets[1].dy,
                    )
                else:
                    self._set_route_offset_values(0.0, 0.0, 0.0, 0.0)
            self._route_table.blockSignals(False)
        finally:
            self._updating_route_controls = False
        self._update_enabled_state()
        self._update_route_array_preview()

    def set_route_measurement_running(self, running: bool) -> None:
        self._route_measurement_running = bool(running)
        if not self._route_measurement_running:
            self._route_measurement_waiting = False
        if self._route_measurement_running:
            self._route_run_status_label.setText("Route measurement running.")
        elif self._route_run_status_label.text() == "Route measurement running.":
            self._route_run_status_label.setText("Route measurement idle.")
        self._update_enabled_state()

    def set_route_measurement_waiting(self, waiting: bool) -> None:
        self._route_measurement_waiting = bool(waiting)
        self._update_enabled_state()

    def set_route_measurement_status(self, text: str) -> None:
        self._route_run_status_label.setText(text or "Route measurement idle.")

    def set_registration_status(self, text: str) -> None:
        self._registration_status_label.setText(text or "No design registration.")

    def set_design_registration_active(self, active: bool) -> None:
        self._design_registration_active = bool(active)
        self._update_enabled_state()

    def set_calibration_prompt(self, text: str) -> None:
        self._calibration_prompt_label.setText(text)

    def set_registration_marks(
        self,
        source_design_marks: list[Point2D | None],
        check_design_marks: list[Point2D],
    ) -> None:
        _ = check_design_marks
        self._source_design_marks = list(source_design_marks[:2])
        while len(self._source_design_marks) < 2:
            self._source_design_marks.append(None)
        self._update_mark_labels()
        self._update_enabled_state()

    def set_stage_registration_marks(self, source_stage_marks: list[Point2D | None]) -> None:
        self._source_stage_marks = list(source_stage_marks[:2])
        while len(self._source_stage_marks) < 2:
            self._source_stage_marks.append(None)
        self._update_mark_labels()
        self._update_enabled_state()

    def set_current_position(
        self,
        stage_xy: Point2D | None,
        design_xy: Point2D | None,
        *,
        fov_design_size: Point2D | None = None,
    ) -> None:
        _ = fov_design_size
        if stage_xy is None:
            self._current_position_label.setText("Stage: unavailable")
            self._current_design_position = None
        elif design_xy is None:
            self._current_design_position = None
            self._current_position_label.setText(
                f"Stage X={stage_xy[0]:.3f}, Y={stage_xy[1]:.3f}"
            )
        else:
            self._current_design_position = design_xy
            self._current_position_label.setText(
                f"Stage X={stage_xy[0]:.3f}, Y={stage_xy[1]:.3f} | "
                f"Design X={design_xy[0]:.3f}, Y={design_xy[1]:.3f}"
            )
        self._update_enabled_state()

    def set_status_message(self, text: str) -> None:
        self._availability_label.setText(text)

    def detach_tool_toolbar(self) -> QWidget:
        self._route_layout.removeWidget(self._tool_toolbar_widget)
        self._tool_toolbar_widget.setParent(None)
        return self._tool_toolbar_widget

    def detach_tool_options_panel(self) -> QWidget:
        self._route_layout.removeWidget(self._tool_group)
        self._tool_group.setParent(None)
        return self._tool_group

    def set_design_dialog_directory(self, directory: str | Path | None) -> None:
        """Update the preferred starting directory for opening GDS files."""

        if directory is None:
            self._design_dialog_directory = ""
            return
        self._design_dialog_directory = str(Path(directory))

    def set_hover_snap(self, snap_result: SnapResult | None) -> None:
        self._update_tool_hover_preview(snap_result)
        if not self._snap_enabled:
            self._snap_hint_label.setText("Snap off: clicks use the exact cursor position.")
            return
        if snap_result is None:
            self._snap_hint_label.setText("Hover snap: move over a line or corner.")
            return
        if snap_result.mode == "free":
            self._snap_hint_label.setText("Hover snap: no nearby geometry, click uses the exact cursor position.")
            return
        label = "Corner" if snap_result.mode == "vertex" else "Line"
        self._snap_hint_label.setText(
            f"Hover snap: {label} at X={snap_result.point[0]:.3f}, "
            f"Y={snap_result.point[1]:.3f} | distance {snap_result.distance:.4f}"
        )

    def _update_mark_labels(self) -> None:
        self._mark_1_label.setText(self._format_mark_label("Mark 1 (LMB)", self._source_design_marks[0]))
        self._mark_2_label.setText(self._format_mark_label("Mark 2 (RMB)", self._source_design_marks[1]))
        self._chip_1_label.setText(self._format_mark_label("Chip 1", self._source_stage_marks[0]))
        self._chip_2_label.setText(self._format_mark_label("Chip 2", self._source_stage_marks[1]))

    def _update_availability(self) -> None:
        messages: list[str] = []
        if pg is None:
            messages.append("pyqtgraph not installed: design window is disabled.")
        else:
            messages.append(
                "Load a GDS for registered navigation. Use Alignment to capture chip points, then click in the design view to move."
            )
        self._availability_label.setText(" ".join(messages))

    def _update_enabled_state(self) -> None:
        has_document = self._document is not None
        self._unload_design_button.setEnabled(has_document)
        self._top_cell_combo.setEnabled(has_document)
        self._layer_list.setEnabled(has_document)
        self._snap_checkbox.setEnabled(has_document)
        self._load_script_button.setEnabled(has_document)
        self._reload_script_button.setEnabled(
            has_document and self._script_label.text() != "No script loaded."
        )
        has_targets = bool(self._targets)
        has_selection = self._selected_target_id is not None
        self._previous_button.setEnabled(has_targets)
        self._next_button.setEnabled(has_targets)
        self._move_button.setEnabled(has_targets and has_selection)
        has_route = self._route is not None
        route_running = self._route_measurement_running
        has_route_selection = (
            has_route
            and 0 <= self._selected_route_point_index < len(self._route.points)
        )
        has_current_design_position = self._current_design_position is not None
        self._route_new_button.setEnabled(has_document and not route_running)
        self._route_open_button.setEnabled(has_document and not route_running)
        self._route_save_button.setEnabled(
            has_route and self._route.path is not None and not route_running
        )
        self._route_save_as_button.setEnabled(has_route and not route_running)
        self._select_tool_button.setEnabled(has_document and not route_running)
        self._ruler_tool_button.setEnabled(has_document and not route_running)
        self._array_tool_button.setEnabled(has_document and not route_running)
        self._rotate_tool_button.setEnabled(
            has_document
            and not route_running
            and not self._design_registration_active
        )
        if not has_document and self._route_pick_mode is not None:
            self._clear_route_pick_mode("Load a design to pick route geometry.")
        if not has_document and self._active_design_tool != "select":
            self._set_design_tool("select")
        for spinbox in (
            self._needle_1_dx_spin,
            self._needle_1_dy_spin,
            self._needle_2_dx_spin,
            self._needle_2_dy_spin,
        ):
            spinbox.setEnabled(has_route and not route_running)
        self._route_table.setEnabled(has_route)
        self._route_add_current_button.setEnabled(
            has_route and has_current_design_position and not route_running
        )
        self._route_remove_button.setEnabled(has_route_selection and not route_running)
        self._route_clear_button.setEnabled(
            has_route and bool(self._route.points) and not route_running
        )
        self._route_run_button.setEnabled(
            has_route and bool(self._route.points) and not route_running
        )
        self._route_stop_button.setEnabled(route_running)
        self._route_interrupt_button.setEnabled(
            route_running and not self._route_measurement_waiting
        )
        self._route_save_shift_button.setEnabled(self._route_measurement_waiting)
        self._route_remeasure_button.setEnabled(self._route_measurement_waiting)
        self._route_skip_button.setEnabled(self._route_measurement_waiting)
        self._route_next_button.setEnabled(self._route_measurement_waiting)
        self._route_jump_selected_button.setEnabled(
            self._route_measurement_waiting and has_route_selection
        )
        for widget in (
            self._ruler_clear_button,
            self._ruler_cancel_button,
            self._route_array_origin_x_spin,
            self._route_array_origin_y_spin,
            self._route_array_dir1_step_x_spin,
            self._route_array_dir1_step_y_spin,
            self._route_array_dir1_count_spin,
            self._route_array_dir2_step_x_spin,
            self._route_array_dir2_step_y_spin,
            self._route_array_dir2_count_spin,
            self._route_array_serpentine_checkbox,
            self._route_array_replace_checkbox,
            self._route_array_pick_origin_button,
            self._route_array_pick_dir1_button,
            self._route_array_pick_extent1_button,
            self._route_array_pick_dir2_button,
            self._route_array_pick_extent2_button,
            self._route_array_create_button,
            self._route_array_cancel_button,
        ):
            widget.setEnabled(has_document and not route_running)

    def _make_route_offset_spinbox(self, parent: QWidget) -> QDoubleSpinBox:
        spinbox = QDoubleSpinBox(parent)
        spinbox.setRange(-1_000_000_000.0, 1_000_000_000.0)
        spinbox.setDecimals(4)
        spinbox.setSingleStep(1.0)
        spinbox.setAlignment(Qt.AlignRight)
        spinbox.setMaximumWidth(96)
        return spinbox

    def _make_route_coordinate_spinbox(self, parent: QWidget) -> QDoubleSpinBox:
        spinbox = QDoubleSpinBox(parent)
        spinbox.setRange(-1_000_000_000.0, 1_000_000_000.0)
        spinbox.setDecimals(4)
        spinbox.setSingleStep(10.0)
        spinbox.setAlignment(Qt.AlignRight)
        spinbox.setMaximumWidth(112)
        return spinbox

    def _make_route_distance_spinbox(self, parent: QWidget) -> QDoubleSpinBox:
        spinbox = QDoubleSpinBox(parent)
        spinbox.setRange(0.0, 1_000_000_000.0)
        spinbox.setDecimals(4)
        spinbox.setSingleStep(10.0)
        spinbox.setAlignment(Qt.AlignRight)
        spinbox.setMaximumWidth(112)
        return spinbox

    def _make_route_angle_spinbox(self, parent: QWidget) -> QDoubleSpinBox:
        spinbox = QDoubleSpinBox(parent)
        spinbox.setRange(-3600.0, 3600.0)
        spinbox.setDecimals(4)
        spinbox.setSingleStep(5.0)
        spinbox.setSuffix(" deg")
        spinbox.setAlignment(Qt.AlignRight)
        spinbox.setMaximumWidth(112)
        return spinbox

    def _make_tool_button(
        self,
        parent: QWidget,
        text: str,
        icon: QIcon,
    ) -> QToolButton:
        button = QToolButton(parent)
        button.setText(text)
        button.setIcon(icon)
        button.setIconSize(QSize(28, 28))
        button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        button.setCheckable(True)
        button.setAutoRaise(True)
        return button

    def _make_icon_button(
        self,
        parent: QWidget,
        text: str,
        icon_name: str,
    ) -> QToolButton:
        button = QToolButton(parent)
        button.setText(text)
        button.setIcon(self._make_tool_icon(icon_name))
        button.setIconSize(QSize(18, 18))
        button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        button.setAutoRaise(False)
        return button

    def _make_tool_icon(self, name: str) -> QIcon:
        pixmap = QPixmap(28, 28)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        dark = QColor("#263238")
        accent = QColor("#0277bd")
        soft = QColor("#ffca28")
        painter.setPen(QPen(dark, 2.0))
        painter.setBrush(Qt.NoBrush)
        if name == "select":
            polygon = QPolygonF(
                [
                    QPointF(7, 4),
                    QPointF(20, 16),
                    QPointF(14, 17),
                    QPointF(17, 25),
                    QPointF(13, 26),
                    QPointF(10, 18),
                    QPointF(6, 23),
                ]
            )
            painter.setBrush(QBrush(QColor("#eceff1")))
            painter.drawPolygon(polygon)
        elif name == "ruler":
            painter.setPen(QPen(accent, 3.0))
            painter.drawLine(QPointF(5, 21), QPointF(23, 7))
            painter.setPen(QPen(dark, 1.5))
            for index in range(5):
                x = 7 + index * 4
                painter.drawLine(QPointF(x, 19 - index * 3), QPointF(x + 2, 21 - index * 3))
        elif name == "array":
            painter.setPen(QPen(accent, 2.0))
            painter.setBrush(QBrush(QColor("#e1f5fe")))
            for row in range(2):
                for column in range(3):
                    painter.drawEllipse(QPointF(8 + column * 7, 9 + row * 8), 2.2, 2.2)
            painter.setBrush(Qt.NoBrush)
            painter.drawLine(QPointF(8, 9), QPointF(22, 17))
        elif name == "rotate":
            painter.save()
            painter.translate(28, 0)
            painter.scale(-1, 1)
            painter.setPen(QPen(accent, 2.4))
            painter.drawArc(5, 5, 18, 18, 35 * 16, 285 * 16)
            painter.drawLine(QPointF(20, 5), QPointF(23, 10))
            painter.drawLine(QPointF(20, 5), QPointF(15, 7))
            painter.setPen(QPen(dark, 1.8))
            painter.drawLine(QPointF(14, 9), QPointF(14, 19))
            painter.drawLine(QPointF(9, 14), QPointF(19, 14))
            painter.restore()
        elif name == "origin":
            painter.setPen(QPen(accent, 2.0))
            painter.drawLine(QPointF(14, 5), QPointF(14, 23))
            painter.drawLine(QPointF(5, 14), QPointF(23, 14))
            painter.setBrush(QBrush(soft))
            painter.drawEllipse(QPointF(14, 14), 3, 3)
        elif name == "direction":
            painter.setPen(QPen(accent, 2.5))
            painter.drawLine(QPointF(5, 20), QPointF(22, 8))
            painter.drawLine(QPointF(22, 8), QPointF(17, 8))
            painter.drawLine(QPointF(22, 8), QPointF(21, 13))
        elif name == "extent":
            painter.setPen(QPen(accent, 2.0))
            painter.drawLine(QPointF(6, 14), QPointF(22, 14))
            painter.drawLine(QPointF(22, 14), QPointF(18, 10))
            painter.drawLine(QPointF(22, 14), QPointF(18, 18))
            painter.setPen(QPen(soft, 2.0))
            painter.drawLine(QPointF(6, 8), QPointF(6, 20))
        elif name == "accept":
            painter.setPen(QPen(QColor("#2e7d32"), 3.0))
            painter.drawLine(QPointF(6, 15), QPointF(12, 21))
            painter.drawLine(QPointF(12, 21), QPointF(23, 7))
        elif name == "cancel":
            painter.setPen(QPen(QColor("#c62828"), 3.0))
            painter.drawLine(QPointF(8, 8), QPointF(21, 21))
            painter.drawLine(QPointF(21, 8), QPointF(8, 21))
        else:
            fallback = self.style().standardIcon(QStyle.SP_FileDialogDetailedView)
            painter.end()
            return fallback
        painter.end()
        return QIcon(pixmap)

    def _set_route_offset_values(
        self,
        needle_1_dx: float,
        needle_1_dy: float,
        needle_2_dx: float,
        needle_2_dy: float,
    ) -> None:
        values = (
            (self._needle_1_dx_spin, needle_1_dx),
            (self._needle_1_dy_spin, needle_1_dy),
            (self._needle_2_dx_spin, needle_2_dx),
            (self._needle_2_dy_spin, needle_2_dy),
        )
        for spinbox, value in values:
            spinbox.blockSignals(True)
            spinbox.setValue(float(value))
            spinbox.blockSignals(False)

    def _emit_route_offsets_changed(self, *_unused: object) -> None:
        if self._updating_route_controls or self._route is None:
            return
        self.route_offsets_changed.emit(
            self._needle_1_dx_spin.value(),
            self._needle_1_dy_spin.value(),
            self._needle_2_dx_spin.value(),
            self._needle_2_dy_spin.value(),
        )
        self._update_route_array_preview()

    def _set_design_tool(self, tool: str) -> None:
        if tool not in {"select", "ruler", "array"}:
            tool = "select"
        self._active_design_tool = tool
        button_by_tool = {
            "select": self._select_tool_button,
            "ruler": self._ruler_tool_button,
            "array": self._array_tool_button,
        }
        for name, button in button_by_tool.items():
            button.blockSignals(True)
            button.setChecked(name == tool)
            button.blockSignals(False)
        stack_index_by_tool = {"select": 0, "ruler": 1, "array": 2}
        self._tool_stack.setCurrentIndex(stack_index_by_tool[tool])
        self._clear_route_pick_mode("")
        self._tool_group.setVisible(tool == "array")
        if tool == "select":
            self.route_preview_changed.emit(None)
            self.tool_measure_preview_changed.emit(None)
            self._tool_status_label.setText("")
        elif tool == "ruler":
            self.route_preview_changed.emit(None)
            self._ruler_anchor = None
            self._ruler_end = None
            self._update_ruler_labels()
            self._tool_status_label.setText("")
            self._route_pick_mode = "ruler"
            self.route_pick_mode_changed.emit("ruler")
        else:
            self.tool_measure_preview_changed.emit(None)
            self._tool_status_label.setText("Adjust array parameters or pick geometry.")
            self._update_route_array_preview()

    def cancel_active_tool(self) -> None:
        if self._active_design_tool == "ruler":
            self._clear_ruler()
            self._set_design_tool("select")
            return
        if self._active_design_tool == "array":
            self._cancel_route_array()
            return
        self._clear_route_pick_mode("")
        self.tool_measure_preview_changed.emit(None)
        self.route_preview_changed.emit(None)

    def _set_route_array_origin(self, point: Point2D) -> None:
        self._route_array_origin_x_spin.setValue(float(point[0]))
        self._route_array_origin_y_spin.setValue(float(point[1]))

    def _clear_ruler(self) -> None:
        self._ruler_anchor = None
        self._ruler_end = None
        self._ruler_segments.clear()
        self._update_ruler_labels()
        self.tool_measure_preview_changed.emit(None)
        self.tool_measurements_changed.emit([])
        if self._active_design_tool == "ruler":
            self._tool_status_label.setText("")

    def _update_ruler_labels(self) -> None:
        self._ruler_start_label.setText(
            "Start: not set"
            if self._ruler_anchor is None
            else f"Start: {self._format_point(self._ruler_anchor)}"
        )
        self._ruler_end_label.setText(
            "End: not set"
            if self._ruler_end is None
            else f"End: {self._format_point(self._ruler_end)}"
        )
        if self._ruler_anchor is None or self._ruler_end is None:
            self._ruler_delta_label.setText("dX=0.000, dY=0.000")
            self._ruler_length_label.setText("Length=0.000, Angle=0.000 deg")
            if self._ruler_segments:
                self._ruler_length_label.setText(
                    f"{len(self._ruler_segments)} measurements"
                )
            return
        dx = self._ruler_end[0] - self._ruler_anchor[0]
        dy = self._ruler_end[1] - self._ruler_anchor[1]
        length = math.hypot(dx, dy)
        angle = math.degrees(math.atan2(dy, dx)) if length > 0.0 else 0.0
        self._ruler_delta_label.setText(f"dX={dx:.3f}, dY={dy:.3f}")
        self._ruler_length_label.setText(f"Length={length:.3f}, Angle={angle:.3f} deg")

    def _start_route_pick_mode(self, mode: str) -> None:
        if self._document is None:
            self._tool_status_label.setText("Load a design to pick geometry.")
            return
        if self._active_design_tool != "array":
            self._set_design_tool("array")
        self._route_pick_mode = mode
        self._route_pick_anchor_mode = None
        self._route_pick_anchor_point = None
        self.tool_measure_preview_changed.emit(None)
        self._tool_status_label.setText(f"Click design for {self._route_pick_label_text(mode)}.")
        self.route_pick_mode_changed.emit(mode)

    def _clear_route_pick_mode(self, status: str = "") -> None:
        self._route_pick_anchor_mode = None
        self._route_pick_anchor_point = None
        if self._route_pick_mode is not None:
            self._route_pick_mode = None
            self.route_pick_mode_changed.emit(None)
        self.tool_measure_preview_changed.emit(None)
        self._tool_status_label.setText(status)

    def apply_route_pick(
        self,
        mode: str,
        x_value: float,
        y_value: float,
    ) -> None:
        point = (float(x_value), float(y_value))
        status = ""
        if mode == "ruler":
            if self._ruler_anchor is None:
                self._ruler_anchor = point
                self._ruler_end = None
                self.tool_measure_preview_changed.emit([point])
            else:
                self._ruler_end = point
                self._ruler_segments.append((self._ruler_anchor, point))
                self.tool_measurements_changed.emit(list(self._ruler_segments))
                self.tool_measure_preview_changed.emit(None)
                self._ruler_anchor = None
                self._ruler_end = None
            self._update_ruler_labels()
            self._route_pick_mode = "ruler"
            self.route_pick_mode_changed.emit("ruler")
            return
        if mode == "array_origin":
            self._set_route_array_origin(point)
            status = f"Array origin set to {self._format_point(point)}."
        elif mode == "array_dir1":
            anchor = self._route_vector_anchor_or_none(mode, point, "Direction 1")
            if anchor is None:
                return
            step = (point[0] - anchor[0], point[1] - anchor[1])
            length, angle = self._vector_length_angle(step)
            self._route_array_dir1_step_x_spin.setValue(length)
            self._route_array_dir1_step_y_spin.setValue(angle)
            status = f"Direction 1 set to length={length:.3f}, angle={angle:.3f} deg."
        elif mode == "array_extent1":
            count = self._count_from_endpoint(
                (
                    self._route_array_origin_x_spin.value(),
                    self._route_array_origin_y_spin.value(),
                ),
                (
                    self._route_array_dir1_step_x_spin.value(),
                    self._route_array_dir1_step_y_spin.value(),
                ),
                point,
            )
            self._route_array_dir1_count_spin.setValue(count)
            status = f"Direction 1 count set to {count}."
        elif mode == "array_dir2":
            anchor = self._route_vector_anchor_or_none(mode, point, "Direction 2")
            if anchor is None:
                return
            step = (point[0] - anchor[0], point[1] - anchor[1])
            length, angle = self._vector_length_angle(step)
            self._route_array_dir2_step_x_spin.setValue(length)
            self._route_array_dir2_step_y_spin.setValue(angle)
            status = f"Direction 2 set to length={length:.3f}, angle={angle:.3f} deg."
        elif mode == "array_extent2":
            count = self._count_from_endpoint(
                (
                    self._route_array_origin_x_spin.value(),
                    self._route_array_origin_y_spin.value(),
                ),
                (
                    self._route_array_dir2_step_x_spin.value(),
                    self._route_array_dir2_step_y_spin.value(),
                ),
                point,
            )
            self._route_array_dir2_count_spin.setValue(count)
            status = f"Direction 2 count set to {count}."
        else:
            status = "Unknown route pick mode."
        self._clear_route_pick_mode(status)
        self._update_route_array_preview()

    def _route_vector_anchor_or_none(
        self,
        mode: str,
        point: Point2D,
        label: str,
    ) -> Point2D | None:
        if self._route_pick_anchor_mode != mode or self._route_pick_anchor_point is None:
            self._route_pick_anchor_mode = mode
            self._route_pick_anchor_point = point
            self.tool_measure_preview_changed.emit([point])
            self._tool_status_label.setText(
                f"{label} vector starts at {self._format_point(point)}; click endpoint."
            )
            return None
        self.tool_measure_preview_changed.emit([self._route_pick_anchor_point, point])
        return self._route_pick_anchor_point

    def _emit_route_array_requested(self) -> None:
        dir1 = self._route_array_dir1_step()
        dir2 = self._route_array_dir2_step()
        self.route_array_requested.emit(
            self._route_array_origin_x_spin.value(),
            self._route_array_origin_y_spin.value(),
            dir1[0],
            dir1[1],
            self._route_array_dir1_count_spin.value(),
            dir2[0],
            dir2[1],
            self._route_array_dir2_count_spin.value(),
            self._route_array_serpentine_checkbox.isChecked(),
            self._route_array_replace_checkbox.isChecked(),
        )
        self._set_design_tool("select")

    def _cancel_route_array(self) -> None:
        self.route_preview_changed.emit(None)
        self._set_design_tool("select")

    def _update_tool_hover_preview(self, snap_result: SnapResult | None) -> None:
        if snap_result is None:
            return
        point = snap_result.point
        if self._active_design_tool == "ruler":
            if self._ruler_anchor is not None and self._ruler_end is None:
                self.tool_measure_preview_changed.emit([self._ruler_anchor, point])
                self._ruler_end = point
                self._update_ruler_labels()
                self._ruler_end = None
            return
        if self._active_design_tool != "array":
            return
        if self._route_pick_mode == "array_origin":
            self._update_route_array_preview(origin_override=point)
        elif self._route_pick_mode == "array_dir1" and self._route_pick_anchor_point is not None:
            step = (
                point[0] - self._route_pick_anchor_point[0],
                point[1] - self._route_pick_anchor_point[1],
            )
            self.tool_measure_preview_changed.emit([self._route_pick_anchor_point, point])
            self._update_route_array_preview(dir1_override=step)
        elif self._route_pick_mode == "array_dir2" and self._route_pick_anchor_point is not None:
            step = (
                point[0] - self._route_pick_anchor_point[0],
                point[1] - self._route_pick_anchor_point[1],
            )
            self.tool_measure_preview_changed.emit([self._route_pick_anchor_point, point])
            self._update_route_array_preview(dir2_override=step)
        elif self._route_pick_mode == "array_extent1":
            count = self._count_from_endpoint(
                self._route_array_origin(),
                self._route_array_dir1_step(),
                point,
            )
            self._update_route_array_preview(count1_override=count)
        elif self._route_pick_mode == "array_extent2":
            count = self._count_from_endpoint(
                self._route_array_origin(),
                self._route_array_dir2_step(),
                point,
            )
            self._update_route_array_preview(count2_override=count)

    def _update_route_array_preview(
        self,
        *_unused: object,
        origin_override: Point2D | None = None,
        dir1_override: Point2D | None = None,
        count1_override: int | None = None,
        dir2_override: Point2D | None = None,
        count2_override: int | None = None,
    ) -> None:
        if self._document is None or self._active_design_tool != "array":
            self.route_preview_changed.emit(None)
            return
        origin = origin_override or self._route_array_origin()
        dir1 = dir1_override or self._route_array_dir1_step()
        dir2 = dir2_override or self._route_array_dir2_step()
        count1 = int(count1_override or self._route_array_dir1_count_spin.value())
        count2 = int(count2_override or self._route_array_dir2_count_spin.value())
        points = self._build_array_preview_points(origin, dir1, count1, dir2, count2)
        self.route_preview_changed.emit((points, self._current_route_offset_vectors()))

    def _build_array_preview_points(
        self,
        origin: Point2D,
        dir1: Point2D,
        count1: int,
        dir2: Point2D,
        count2: int,
    ) -> list[Point2D]:
        count1 = max(1, int(count1))
        count2 = max(1, int(count2))
        dir1_x, dir1_y = float(dir1[0]), float(dir1[1])
        dir2_x, dir2_y = float(dir2[0]), float(dir2[1])
        if count1 > 1 and abs(dir1_x) <= 1e-12 and abs(dir1_y) <= 1e-12:
            return []
        if count2 > 1 and abs(dir2_x) <= 1e-12 and abs(dir2_y) <= 1e-12:
            return []
        points: list[Point2D] = []
        for row_index in range(count2):
            if self._route_array_serpentine_checkbox.isChecked() and row_index % 2 == 1:
                column_indices = range(count1 - 1, -1, -1)
            else:
                column_indices = range(count1)
            for column_index in column_indices:
                points.append(
                    (
                        float(origin[0])
                        + dir1_x * float(column_index)
                        + dir2_x * float(row_index),
                        float(origin[1])
                        + dir1_y * float(column_index)
                        + dir2_y * float(row_index),
                    )
                )
        return points

    def _route_array_origin(self) -> Point2D:
        return (
            self._route_array_origin_x_spin.value(),
            self._route_array_origin_y_spin.value(),
        )

    def _route_array_dir1_step(self) -> Point2D:
        return self._vector_from_length_angle(
            self._route_array_dir1_step_x_spin.value(),
            self._route_array_dir1_step_y_spin.value(),
        )

    def _route_array_dir2_step(self) -> Point2D:
        return self._vector_from_length_angle(
            self._route_array_dir2_step_x_spin.value(),
            self._route_array_dir2_step_y_spin.value(),
        )

    def _current_route_offset_vectors(self) -> list[Point2D]:
        if self._route is not None and len(self._route.needle_offsets) >= 2:
            return [
                (self._route.needle_offsets[0].dx, self._route.needle_offsets[0].dy),
                (self._route.needle_offsets[1].dx, self._route.needle_offsets[1].dy),
            ]
        return [
            (self._needle_1_dx_spin.value(), self._needle_1_dy_spin.value()),
            (self._needle_2_dx_spin.value(), self._needle_2_dy_spin.value()),
        ]

    @staticmethod
    def _vector_from_length_angle(length: float, angle_degrees: float) -> Point2D:
        radians = math.radians(float(angle_degrees))
        distance = float(length)
        return (distance * math.cos(radians), distance * math.sin(radians))

    @staticmethod
    def _vector_length_angle(vector: Point2D) -> tuple[float, float]:
        dx = float(vector[0])
        dy = float(vector[1])
        length = math.hypot(dx, dy)
        angle = math.degrees(math.atan2(dy, dx)) if length > 0.0 else 0.0
        return length, angle

    @staticmethod
    def _count_from_endpoint(
        start: Point2D,
        step: Point2D,
        end: Point2D,
    ) -> int:
        step_x = float(step[0])
        step_y = float(step[1])
        denominator = step_x * step_x + step_y * step_y
        if denominator <= 1e-18:
            return 1
        delta_x = float(end[0]) - float(start[0])
        delta_y = float(end[1]) - float(start[1])
        projected_steps = (delta_x * step_x + delta_y * step_y) / denominator
        return max(1, int(math.floor(projected_steps + 0.5)) + 1)

    @staticmethod
    def _route_pick_label_text(mode: str) -> str:
        labels = {
            "array_origin": "array origin",
            "array_dir1": "direction 1 vector start",
            "array_extent1": "direction 1 approximate end",
            "array_dir2": "direction 2 vector start",
            "array_extent2": "direction 2 approximate end",
        }
        return labels.get(mode, "route point")

    def _choose_design_file(self) -> None:  # pragma: no cover - UI interaction
        start_directory = self._design_dialog_directory
        if not start_directory and self._document is not None:
            start_directory = str(self._document.path.parent)
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Design",
            start_directory,
            "Layout files (*.gds *.gdsii *.oas *.oasis);;All files (*)",
        )
        if path:
            self.load_design_requested.emit(path)

    def _choose_script_file(self) -> None:  # pragma: no cover - UI interaction
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Measurement Plan",
            "",
            "Python files (*.py);;All files (*)",
        )
        if path:
            self.load_script_requested.emit(path)

    def _choose_route_file(self) -> None:  # pragma: no cover - UI interaction
        start_directory = ""
        if self._route is not None and self._route.path is not None:
            start_directory = str(self._route.path.parent)
        elif self._document is not None:
            start_directory = str(self._document.path.parent)
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Probe Route",
            start_directory,
            "Probe routes (*.probe-route.json *.json);;All files (*)",
        )
        if path:
            self.route_open_requested.emit(path)

    def _choose_route_save_file(self) -> None:  # pragma: no cover - UI interaction
        if self._route is None:
            return
        start_directory = ""
        if self._route.path is not None:
            start_directory = str(self._route.path.parent)
        elif self._document is not None:
            start_directory = str(self._document.path.parent)
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Probe Route",
            start_directory,
            "Probe routes (*.probe-route.json);;JSON files (*.json);;All files (*)",
        )
        if path:
            self.route_save_as_requested.emit(path)

    def _on_top_cell_changed(self, cell_name: str) -> None:
        if self._document is None or not cell_name:
            return
        if cell_name == self._document.top_cell_name:
            return
        self.top_cell_changed.emit(cell_name)

    def set_snap_enabled(self, enabled: bool) -> None:
        """Update the visible snap toggle state without re-emitting it."""

        self._snap_enabled = bool(enabled)
        self._snap_checkbox.blockSignals(True)
        self._snap_checkbox.setChecked(self._snap_enabled)
        self._snap_checkbox.blockSignals(False)
        self.set_hover_snap(None)

    def _on_snap_checkbox_toggled(self, checked: bool) -> None:
        self._snap_enabled = bool(checked)
        self.set_hover_snap(None)
        self.snap_enabled_changed.emit(self._snap_enabled)

    def _on_layer_item_changed(self, item: QListWidgetItem) -> None:
        layer_key = item.data(Qt.UserRole)
        if not isinstance(layer_key, tuple) or len(layer_key) != 2:
            return
        self.layer_visibility_changed.emit(
            int(layer_key[0]),
            int(layer_key[1]),
            item.checkState() == Qt.Checked,
        )

    def _on_target_selection_changed(self) -> None:
        selected_rows = self._target_table.selectionModel().selectedRows()
        if not selected_rows:
            self._selected_target_id = None
            self._update_enabled_state()
            return
        row = selected_rows[0].row()
        if row < 0 or row >= len(self._targets):
            return
        target = self._targets[row]
        self._selected_target_id = target.id
        self.target_selected.emit(target.id)
        self._update_enabled_state()

    def _on_route_selection_changed(self) -> None:
        selected_rows = self._route_table.selectionModel().selectedRows()
        if not selected_rows:
            self._selected_route_point_index = -1
            self.route_selected.emit(-1)
            self._update_enabled_state()
            return
        row = selected_rows[0].row()
        self._selected_route_point_index = row
        self.route_selected.emit(row)
        self._update_enabled_state()

    def _emit_move_to_selected_target(self) -> None:
        if self._selected_target_id is None:
            return
        self.move_to_target_requested.emit(self._selected_target_id)

    def _emit_route_measurement_jump_to_selected(self) -> None:
        if self._selected_route_point_index < 0:
            return
        self.route_measurement_jump_requested.emit(
            self._selected_route_point_index + 1
        )

    @staticmethod
    def _format_bounds(bounds: tuple[float, float, float, float]) -> str:
        left, bottom, right, top = bounds
        width = right - left
        height = top - bottom
        diagonal = math.hypot(width, height)
        return f"{width:.3f} x {height:.3f} (diag {diagonal:.3f})"

    @staticmethod
    def _format_mark_label(label: str, point: Point2D | None) -> str:
        if point is None:
            return f"{label}: not set"
        return f"{label}: X={point[0]:.3f}, Y={point[1]:.3f}"

    @staticmethod
    def _format_point(point: Point2D) -> str:
        return f"X={point[0]:.3f}, Y={point[1]:.3f}"


class DesignLayoutWindow(QWidget):
    """Top-level design window combining the layout view and design controls."""

    calibration_point_selected = Signal(int, float, float)
    move_requested = Signal(float, float)
    route_point_requested = Signal(float, float)
    hover_snap_changed = Signal(object)
    visibility_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        _ = parent
        super().__init__(None)
        self.setWindowTitle("Design Window")
        self.setWindowFlag(Qt.Window, True)
        self.resize(1480, 920)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.setSpacing(8)

        self._main_view = _DesignPlotPane(parent=self)
        self.navigator_panel = DesignNavigatorPanel(self)
        tool_toolbar = self.navigator_panel.detach_tool_toolbar()
        tool_toolbar.setObjectName("DesignToolToolbar")
        tool_toolbar.setMinimumHeight(52)
        tool_options = self.navigator_panel.detach_tool_options_panel()
        tool_options.setMinimumWidth(300)
        tool_options.setMaximumWidth(360)
        self.navigator_panel.setMinimumWidth(400)
        navigator_scroll = QScrollArea(self)
        navigator_scroll.setWidgetResizable(True)
        navigator_scroll.setFrameShape(QScrollArea.NoFrame)
        navigator_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        navigator_scroll.setWidget(self.navigator_panel)
        navigator_scroll.setMinimumWidth(430)
        self._main_view.calibration_point_selected.connect(
            self.calibration_point_selected.emit
        )
        self._main_view.move_requested.connect(self.move_requested.emit)
        self._main_view.route_point_requested.connect(self.route_point_requested.emit)
        self._main_view.route_pick_requested.connect(
            self.navigator_panel.apply_route_pick
        )
        self._main_view.hover_snap_changed.connect(self.hover_snap_changed.emit)
        self.navigator_panel.route_pick_mode_changed.connect(
            self._main_view.set_route_pick_mode
        )
        self.navigator_panel.route_preview_changed.connect(
            self._main_view.set_probe_route_preview
        )
        self.navigator_panel.tool_measure_preview_changed.connect(
            self._main_view.set_tool_measure_points
        )
        self.navigator_panel.tool_measurements_changed.connect(
            self._main_view.set_tool_measure_segments
        )
        self._escape_shortcut = QShortcut(QKeySequence("Esc"), self)
        self._escape_shortcut.setContext(Qt.WindowShortcut)
        self._escape_shortcut.activated.connect(self.navigator_panel.cancel_active_tool)

        content_layout = QHBoxLayout()
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(8)
        content_layout.addWidget(tool_options, 0)
        content_layout.addWidget(self._main_view, 1)
        content_layout.addWidget(navigator_scroll, 0)
        root_layout.addWidget(tool_toolbar, 0)
        root_layout.addLayout(content_layout, 1)

    def set_document(self, document: DesignDocument | None) -> None:
        self._main_view.set_document(document)
        self.navigator_panel.set_document(document)

    def set_targets(
        self,
        targets: list[MeasurementTarget],
        *,
        selected_target_id: str | None,
    ) -> None:
        self._main_view.set_targets(targets, selected_target_id=selected_target_id)
        self.navigator_panel.set_targets(targets, selected_target_id=selected_target_id)

    def set_probe_route(
        self,
        route: MeasurementRoute | None,
        *,
        selected_route_point_index: int,
    ) -> None:
        self._main_view.set_probe_route(
            route,
            selected_route_point_index=selected_route_point_index,
        )
        self.navigator_panel.set_route(
            route,
            selected_route_point_index=selected_route_point_index,
        )

    def set_registration_marks(
        self,
        source_design_marks: list[Point2D | None],
        check_design_marks: list[Point2D],
    ) -> None:
        self._main_view.set_registration_marks(source_design_marks, check_design_marks)
        self.navigator_panel.set_registration_marks(source_design_marks, check_design_marks)

    def set_navigation_enabled(self, enabled: bool) -> None:
        """Toggle click-to-move behavior in the design plot."""

        self._main_view.set_navigation_enabled(enabled)

    def set_route_edit_enabled(self, enabled: bool) -> None:
        """Toggle route point placement in the design plot."""

        self._main_view.set_route_edit_enabled(enabled)

    def set_snap_enabled(self, enabled: bool) -> None:
        """Toggle geometry snapping in the design plot and sidebar."""

        self._main_view.set_snap_enabled(enabled)
        self.navigator_panel.set_snap_enabled(enabled)

    def set_stage_registration_marks(self, source_stage_marks: list[Point2D | None]) -> None:
        self.navigator_panel.set_stage_registration_marks(source_stage_marks)

    def set_script_path(self, script_path: str | None) -> None:
        self.navigator_panel.set_script_path(script_path)

    def set_calibration_prompt(self, text: str) -> None:
        self.navigator_panel.set_calibration_prompt(text)

    def set_registration_status(self, text: str) -> None:
        self.navigator_panel.set_registration_status(text)

    def set_status_message(self, text: str) -> None:
        self._main_view.set_status_message(text)
        self.navigator_panel.set_status_message(text)

    def set_hover_snap(self, snap_result: SnapResult | None) -> None:
        self.navigator_panel.set_hover_snap(snap_result)

    def set_current_position(
        self,
        stage_xy: Point2D | None,
        design_xy: Point2D | None,
        *,
        fov_design_size: Point2D | None = None,
    ) -> None:
        self.navigator_panel.set_current_position(
            stage_xy,
            design_xy,
            fov_design_size=fov_design_size,
        )

    def set_current_design_position(
        self,
        point: Point2D | None,
        *,
        fov_design_size: Point2D | None = None,
    ) -> None:
        self._main_view.set_current_design_position(point, fov_design_size=fov_design_size)

    def show_and_raise(self) -> None:
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.setWindowState(self.windowState() | Qt.WindowActive)
        self.raise_()
        self.activateWindow()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def hideEvent(self, event) -> None:  # type: ignore[override]
        super().hideEvent(event)
        self.visibility_changed.emit(False)


__all__ = ["DesignLayoutWindow", "DesignNavigatorPanel"]
