"""Pyqtgraph design plot pane for the design navigator."""

from __future__ import annotations

import logging
import math
import os
import threading
from time import perf_counter, monotonic

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from probe_station_gui.design.markup import MarkupDocument
from probe_station_gui.design.navigation_bounds import (
    Box2D,
    GDS_FOCUS_PADDING_FRACTION,
    VIEW_RANGE_ABS_TOLERANCE,
    clamp_view_bounds,
    content_bounds,
    fit_bounds_to_aspect,
    navigation_frame,
    pad_bounds,
)

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
    RenderFailure,
    SnapFailure,
    SnapRequest,
    SnapResponse,
)
from probe_station_gui.design.klayout_workers import KLayoutSnapWorker
from probe_station_gui.design.selection_geometry import (
    GuideSnapCandidate,
    SegmentGeometry,
    SelectionRect,
    constrain_vector_endpoint,
    guide_snap_candidates,
)
from probe_station_gui.design.selection_model import (
    EntityOwner,
    SelectableDesignEntity,
    SelectionModel,
    entities_in_rect,
    route_entity_id,
)
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

DesignContentKey = tuple[str, str, int, Box2D, str | None]


def _design_content_key(document: DesignDocument) -> DesignContentKey:
    return (
        os.path.normcase(os.path.abspath(os.fspath(document.path))),
        str(document.top_cell_name),
        int(document.rotation_quarter_turns) % 4,
        tuple(float(value) for value in document.bounds),
        document.source_load_id,
    )


def _route_matches_document(
    route: MeasurementRoute,
    document: DesignDocument,
) -> bool:
    same_path = os.path.normcase(os.path.abspath(route.design.path)) == os.path.normcase(
        os.path.abspath(os.fspath(document.path))
    )
    return (
        same_path
        and route.design.top_cell_name == str(document.top_cell_name)
        and all(
            math.isclose(route_value, document_value, rel_tol=0.0, abs_tol=1e-9)
            for route_value, document_value in zip(
                route.design.bounds,
                document.bounds,
                strict=True,
            )
        )
        and math.isclose(route.design.dbu, document.dbu, rel_tol=0.0, abs_tol=1e-12)
    )


def _markup_matches_document(
    markup: MarkupDocument,
    document: DesignDocument,
) -> bool:
    return markup.source_path == os.path.normcase(
        os.path.abspath(os.fspath(document.path))
    )


class _DesignPlotPane(QWidget):
    """Thin wrapper around pyqtgraph for design rendering."""

    calibration_point_selected = Signal(int, float, float)
    move_requested = Signal(float, float)
    route_point_requested = Signal(float, float)
    point_requested = Signal(float, float)
    alignment_point_requested = Signal(float, float)
    guide_requested = Signal(object, object)
    entity_selection_requested = Signal(object, str)
    route_pick_requested = Signal(str, float, float, bool, bool)
    hover_snap_changed = Signal(object)
    tool_hover_snap_changed = Signal(object, bool, bool)
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
        self._navigation_content_bounds: Box2D | None = None
        self._navigation_frame: Box2D | None = None
        self._navigation_ignored_coordinate_count = 0
        self._last_navigation_invalid_coordinate_count = 0
        self._targets: list[MeasurementTarget] = []
        self._selected_target_id: str | None = None
        self._probe_route: MeasurementRoute | None = None
        self._probe_route_document_key: DesignContentKey | None = None
        self._selected_route_point_index = -1
        self._probe_route_preview_points: list[Point2D] = []
        self._probe_route_preview_offsets: list[Point2D] = []
        self._tool_measure_segments: list[tuple[Point2D, Point2D]] = []
        self._tool_measure_points: list[Point2D] = []
        self._tool_measure_label_items: list[object] = []
        self._alignment_draft_points: list[Point2D] = []
        self._alignment_draft_label_items: list[object] = []
        self._tool_sketch_segments: list[tuple[Point2D, Point2D]] = []
        self._tool_sketch_points: list[Point2D] = []
        self._markup: MarkupDocument | None = None
        self._markup_document_key: DesignContentKey | None = None
        self._markup_generation = 0
        self._markup_snap_candidates: tuple[GuideSnapCandidate, ...] = ()
        self._selectable_entities: tuple[SelectableDesignEntity, ...] = ()
        self._selection = SelectionModel()
        self._selection_managed = False
        self._active_design_tool = "legacy"
        self._guide_anchor: Point2D | None = None
        self._mixed_preview_points: list[Point2D] = []
        self._mixed_preview_segments: list[SegmentGeometry] = []
        self._selection_rect_mode: str | None = None
        self._select_press_scene_pos: QPointF | None = None
        self._select_press_design_point: Point2D | None = None
        self._select_press_entity_id: str | None = None
        self._select_dragging = False
        self._select_modifiers = Qt.NoModifier
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
        self._pending_hover_markup: dict[
            int,
            tuple[SnapResult | None, bool, bool],
        ] = {}
        self._snap_worker: KLayoutSnapWorker | None = None
        self._snap_worker_source_key = None
        self._retired_snap_workers: set[object] = set()
        self._snap_retirement_callbacks: dict[object, object] = {}
        self._klayout_config: KLayoutConfig | None = None
        self._raster_item: KLayoutRasterItem | None = None
        self._raster_controller: KLayoutRasterController | None = None
        self._shutdown = False
        self._render_error_visible = False
        self._snap_failure_visible = False
        self._plot = None
        self._status_label: QLabel | None = None
        self._route_geometry_redraw_timer: QTimer | None = None
        self._route_geometry_deferred = False
        self._document_preview_active = False
        self._document_preview_previous_document: DesignDocument | None = None
        self._document_preview_previous_view_range: tuple[
            tuple[float, float],
            tuple[float, float],
        ] | None = None
        self._preview_overlay_items: tuple[object, ...] = ()

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
        self._raster_controller.succeeded.connect(self._on_klayout_render_succeeded)

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
        self._tool_sketch_intersection_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ffee58", width=1.4),
            brush=pg.mkBrush(255, 238, 88, 100),
            size=9,
            symbol="x",
        )
        self._markup_selected_item = self._plot.plot(
            [],
            [],
            pen=pg.mkPen("#ff7043", width=3.0, style=Qt.DashLine),
        )
        self._mixed_preview_segment_item = self._plot.plot(
            [],
            [],
            pen=pg.mkPen("#ffca28", width=2.0, style=Qt.DashLine),
        )
        self._mixed_preview_point_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ffca28", width=1.5),
            brush=pg.mkBrush(255, 202, 40, 100),
            size=9,
            symbol="o",
        )
        self._selection_rect_item = self._plot.plot([], [])
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
        self._alignment_draft_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#00bcd4", width=2),
            brush=pg.mkBrush(0, 188, 212, 150),
            size=11,
            symbol="o",
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
        self._plot.addItem(self._tool_sketch_intersection_item)
        self._plot.addItem(self._mixed_preview_point_item)
        self._plot.addItem(self._axis_triad_x_label)
        self._plot.addItem(self._axis_triad_y_label)
        self._plot.addItem(self._axis_triad_z_label)
        self._plot.addItem(self._axis_triad_z_dot_item)
        self._plot.addItem(self._selected_target_item)
        self._plot.addItem(self._current_item)
        self._plot.addItem(self._source_mark_1_item)
        self._plot.addItem(self._source_mark_2_item)
        self._plot.addItem(self._alignment_draft_item)
        self._plot.addItem(self._check_mark_item)
        self._preview_overlay_items = tuple(
            item
            for name, item in vars(self).items()
            if name.endswith("_item")
            and name != "_raster_item"
            and hasattr(item, "setVisible")
        )
        self._plot.scene().sigMouseClicked.connect(self._on_mouse_clicked)
        self._plot.scene().sigMouseMoved.connect(self._on_mouse_moved)
        self._plot.scene().installEventFilter(self)
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
        self._update_navigation_limits()

    def _viewport_size(self) -> tuple[float, float]:
        if self._plot is None:
            return (1.0, 1.0)
        rect = self._plot.getViewBox().sceneBoundingRect()
        return (max(1.0, float(rect.width())), max(1.0, float(rect.height())))

    def _current_view_bounds(self) -> Box2D | None:
        if self._plot is None:
            return None
        try:
            x_range, y_range = self._plot.getViewBox().viewRange()[:2]
            values = (
                float(x_range[0]),
                float(y_range[0]),
                float(x_range[1]),
                float(y_range[1]),
            )
        except (AttributeError, IndexError, TypeError, ValueError):
            return None
        return values if all(math.isfinite(value) for value in values) else None

    def _set_view_bounds(self, bounds: Box2D) -> None:
        if self._plot is None:
            return
        left, bottom, right, top = bounds
        self._plot.getViewBox().setRange(
            xRange=(left, right),
            yRange=(bottom, top),
            padding=0.0,
        )

    def _clear_navigation_limits(self) -> None:
        self._navigation_content_bounds = None
        self._navigation_frame = None
        self._navigation_ignored_coordinate_count = 0
        self._last_navigation_invalid_coordinate_count = 0
        if self._plot is not None:
            self._plot.getViewBox().setLimits(
                xMin=None,
                xMax=None,
                yMin=None,
                yMax=None,
                maxXRange=None,
                maxYRange=None,
            )

    def _update_navigation_limits(
        self,
        *,
        recompute_content: bool = False,
        focus_gds: bool = False,
    ) -> None:
        if self._plot is None or self._document is None:
            self._clear_navigation_limits()
            return
        if recompute_content or self._navigation_content_bounds is None:
            identity = _design_content_key(self._document)
            route = (
                self._probe_route
                if not self._document_preview_active
                and self._probe_route_document_key == identity
                else None
            )
            markup = (
                self._markup
                if not self._document_preview_active
                and self._markup_document_key == identity
                else None
            )
            (
                self._navigation_content_bounds,
                self._navigation_ignored_coordinate_count,
            ) = content_bounds(self._document.bounds, route, markup)
        cached_content = self._navigation_content_bounds
        if cached_content is None:
            return
        self._navigation_frame = navigation_frame(
            cached_content,
            self._viewport_size(),
        )
        left, bottom, right, top = self._navigation_frame
        view_box = self._plot.getViewBox()
        view_box.setLimits(
            xMin=left,
            xMax=right,
            yMin=bottom,
            yMax=top,
            maxXRange=right - left,
            maxYRange=top - bottom,
        )
        if (
            self._navigation_ignored_coordinate_count
            and self._navigation_ignored_coordinate_count
            != self._last_navigation_invalid_coordinate_count
        ):
            logger.warning(
                "Ignored %d invalid design navigation coordinates.",
                self._navigation_ignored_coordinate_count,
            )
        self._last_navigation_invalid_coordinate_count = (
            self._navigation_ignored_coordinate_count
        )
        if focus_gds:
            self.focus_gds_bounds()
            return
        current = self._current_view_bounds()
        if current is not None:
            clamped = clamp_view_bounds(current, self._navigation_frame)
            if (
                current[0] < self._navigation_frame[0]
                or current[1] < self._navigation_frame[1]
                or current[2] > self._navigation_frame[2]
                or current[3] > self._navigation_frame[3]
            ) and clamped == self._navigation_frame:
                epsilon = VIEW_RANGE_ABS_TOLERANCE
                clamped = (
                    clamped[0] + epsilon,
                    clamped[1] + epsilon,
                    clamped[2] - epsilon,
                    clamped[3] - epsilon,
                )
            if any(
                abs(a - b) > VIEW_RANGE_ABS_TOLERANCE
                for a, b in zip(current, clamped, strict=True)
            ):
                self._set_view_bounds(clamped)

    def focus_gds_bounds(self) -> None:
        if self._document is None:
            return
        focused = fit_bounds_to_aspect(
            pad_bounds(self._document.bounds, GDS_FOCUS_PADDING_FRACTION),
            self._viewport_size(),
        )
        if self._navigation_frame is not None:
            focused = clamp_view_bounds(focused, self._navigation_frame)
        self._set_view_bounds(focused)

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
            self._pending_hover_markup.clear()
            self._set_hover_snap(None)
            self._detach_file_backed_document(timeout_s=0.0)
            self._clear_navigation_limits()
            self.set_status_message("No design loaded.")
            self._redraw_document()
        elif not same_document:
            self._snap_generation += 1
            self._latest_hover_request_id = 0
            self._pending_clicks.clear()
            self._pending_hover_markup.clear()
            self._set_hover_snap(None)
            self.set_status_message("")
            self._update_navigation_limits(recompute_content=True, focus_gds=True)
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
        source_key = (config.path, config.source_load_id)
        if (
            self._snap_worker is not None
            and self._snap_worker_source_key == source_key
        ):
            return
        self._stop_snap_worker(timeout_s=0.0)
        worker = KLayoutSnapWorker()
        worker.snap_ready.connect(self._on_file_backed_snap_ready)
        worker.failed.connect(self._on_klayout_snap_failed)
        self._snap_worker = worker
        self._snap_worker_source_key = source_key

    def _detach_file_backed_document(self, *, timeout_s: float) -> None:
        self._klayout_config = None
        self._latest_hover_request_id = 0
        self._pending_clicks.clear()
        self._pending_hover_markup.clear()
        self._stop_snap_worker(timeout_s=timeout_s)
        if self._raster_controller is not None:
            self._raster_controller.set_document(None)

    def _stop_snap_worker(self, *, timeout_s: float) -> None:
        worker = self._snap_worker
        self._snap_worker = None
        self._snap_worker_source_key = None
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
        finished = getattr(worker, "finished", None)
        if finished is None:
            worker.stop(timeout_s=timeout_s)
            worker.deleteLater()
            return
        def callback(worker: object = worker) -> None:
            self._finalize_retired_snap_worker(worker)

        self._retired_snap_workers.add(worker)
        self._snap_retirement_callbacks[worker] = callback
        finished.connect(callback)
        worker.stop(timeout_s=timeout_s)
        if bool(getattr(worker, "is_finished", False)):
            self._finalize_retired_snap_worker(worker)

    def _finalize_retired_snap_worker(self, worker: object) -> None:
        if worker not in self._retired_snap_workers:
            return
        callback = self._snap_retirement_callbacks.pop(worker, None)
        finished = getattr(worker, "finished", None)
        if callback is not None and finished is not None:
            try:
                finished.disconnect(callback)
            except (RuntimeError, TypeError):
                pass
        self._retired_snap_workers.discard(worker)
        worker.deleteLater()

    def _on_klayout_render_failed(self, failure: RenderFailure) -> None:
        logger.warning("KLayout design render failed: %s", failure.message)
        self._render_error_visible = True
        self.set_status_message("Design rendering failed.")

    def _on_klayout_render_succeeded(self) -> None:
        if not self._render_error_visible:
            return
        self._render_error_visible = False
        if not self._snap_failure_visible:
            self.set_status_message("")

    def _on_klayout_snap_failed(self, failure: SnapFailure) -> None:
        config = self._klayout_config
        if config is None or failure.config_generation != config.generation:
            return
        logger.warning("KLayout design snap failed: %s", failure.message)
        if failure.purpose == "hover":
            if failure.request_id != self._latest_hover_request_id:
                return
            self._latest_hover_request_id = 0
            self._pending_hover_markup.pop(failure.request_id, None)
            self._set_hover_snap(None)
            return
        if failure.purpose != "click":
            return
        pending = self._pending_clicks.get(failure.request_id)
        if pending is None or pending.config_generation != config.generation:
            return
        self._pending_clicks.pop(failure.request_id, None)
        self._snap_failure_visible = True
        self.set_status_message("Snap failed. Try again.")
        QTimer.singleShot(1500, self._clear_snap_failure_status)

    def _clear_snap_failure_status(self) -> None:
        if not self._snap_failure_visible:
            return
        self._snap_failure_visible = False
        if not self._render_error_visible:
            self.set_status_message("")

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
            layout = self.layout()
            if layout is not None:
                layout.activate()
            self._plot.resize(self.contentsRect().size())
            self._plot.plotItem.setGeometry(QRectF(self._plot.rect()))

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
        self._probe_route_document_key = (
            _design_content_key(self._document)
            if self._document is not None
            and route is not None
            and _route_matches_document(route, self._document)
            else None
        )
        self._update_navigation_limits(recompute_content=True)
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

    def set_document_preview(self, document: DesignDocument) -> None:
        if not self._document_preview_active:
            self._document_preview_previous_document = self._document
            if self._plot is not None:
                view_range = self._plot.getViewBox().viewRange()
                self._document_preview_previous_view_range = (
                    (float(view_range[0][0]), float(view_range[0][1])),
                    (float(view_range[1][0]), float(view_range[1][1])),
                )
        self._document_preview_active = True
        self.set_document(document)
        self._update_navigation_limits(recompute_content=True, focus_gds=True)
        self._set_preview_overlay_visibility(False)
        self.set_status_message("")

    def finish_document_preview(
        self,
        document: DesignDocument | None,
    ) -> None:
        restore_view = (
            document is self._document_preview_previous_document
            and self._document_preview_previous_view_range is not None
        )
        previous_view_range = self._document_preview_previous_view_range
        self._document_preview_active = False
        self.set_document(document)
        self._update_navigation_limits(recompute_content=True)
        if restore_view and self._plot is not None and previous_view_range is not None:
            self._plot.getViewBox().setRange(
                xRange=previous_view_range[0],
                yRange=previous_view_range[1],
                padding=0.0,
            )
        self._document_preview_previous_document = None
        self._document_preview_previous_view_range = None
        self._set_preview_overlay_visibility(True)
        self._redraw_overlays()
        if document is not None:
            self.set_status_message("")

    def _set_preview_overlay_visibility(self, visible: bool) -> None:
        for item in (
            *self._preview_overlay_items,
            *self._probe_route_number_items,
            *self._tool_measure_label_items,
            *self._alignment_draft_label_items,
        ):
            item.setVisible(bool(visible))

    @property
    def active_design_tool(self) -> str:
        return self._active_design_tool

    def set_active_design_tool(self, tool: str) -> None:
        normalized = str(tool).strip().lower()
        if normalized not in {
            "select",
            "point",
            "guide",
            "ruler",
            "array",
            "align",
            "legacy",
        }:
            raise ValueError(f"Unknown design tool {tool!r}.")
        if normalized != "guide":
            self._guide_anchor = None
            self._tool_sketch_points = []
        self._active_design_tool = normalized
        self._clear_selection_interaction()
        if self._plot is not None:
            cursor = (
                Qt.CrossCursor
                if normalized in {"point", "guide", "ruler", "array", "align"}
                else Qt.ArrowCursor
            )
            self._plot.setCursor(cursor)
        self._redraw_overlays()

    def set_markup(self, markup: MarkupDocument | None) -> None:
        if markup != self._markup:
            self._markup_generation += 1
            self._pending_hover_markup = {
                request_id: (None, shift, control)
                for request_id, (_result, shift, control) in self._pending_hover_markup.items()
            }
        self._markup = markup
        self._markup_document_key = (
            _design_content_key(self._document)
            if self._document is not None
            and markup is not None
            and _markup_matches_document(markup, self._document)
            else None
        )
        self._update_navigation_limits(recompute_content=True)
        if markup is None or not markup.visible:
            self._tool_sketch_segments = []
            self._markup_snap_candidates = ()
            self._selectable_entities = tuple(
                entity
                for entity in self._selectable_entities
                if entity.owner is not EntityOwner.MARKUP
            )
            self._selection = self._selection.prune(
                entity.id for entity in self._selectable_entities
            )
        else:
            geometries = tuple(guide.geometry() for guide in markup.guides)
            self._tool_sketch_segments = [
                (geometry.start, geometry.end) for geometry in geometries
            ]
            self._markup_snap_candidates = guide_snap_candidates(geometries)
        self._redraw_overlays()

    def set_selectable_entities(
        self,
        entities: object,
    ) -> None:
        if isinstance(entities, (list, tuple)) and all(
            isinstance(entity, SelectableDesignEntity) for entity in entities
        ):
            self._selectable_entities = tuple(entities)
        else:
            self._selectable_entities = ()
        valid_ids = {entity.id for entity in self._selectable_entities}
        self._selection = self._selection.prune(valid_ids)
        self._redraw_overlays()

    def set_selection(self, selection: SelectionModel) -> None:
        valid_ids = {entity.id for entity in self._selectable_entities}
        self._selection = selection.prune(valid_ids)
        self._selection_managed = True
        self._redraw_overlays()

    def set_mixed_array_preview(
        self,
        points: object,
        segments: object,
    ) -> None:
        self._mixed_preview_points = [
            (float(point[0]), float(point[1]))
            for point in points
            if isinstance(point, (list, tuple)) and len(point) == 2
        ] if isinstance(points, (list, tuple)) else []
        self._mixed_preview_segments = [
            segment for segment in segments if isinstance(segment, SegmentGeometry)
        ] if isinstance(segments, (list, tuple)) else []
        self._redraw_mixed_preview()

    def cancel_active_interaction(self) -> None:
        self._cancel_pending_tool_snaps()
        self._guide_anchor = None
        self._tool_sketch_points = []
        self._clear_selection_interaction()
        self._redraw_overlays()

    def _cancel_pending_tool_snaps(self) -> None:
        self._latest_hover_request_id = 0
        self._pending_hover_markup.clear()
        self._pending_clicks = {
            request_id: pending
            for request_id, pending in self._pending_clicks.items()
            if pending.action
            not in {"point", "guide_point", "route_pick", "alignment_point"}
        }

    @staticmethod
    def _constraint_flags(
        modifiers: Qt.KeyboardModifiers,
    ) -> tuple[bool, bool]:
        return (
            bool(modifiers & Qt.ShiftModifier),
            bool(modifiers & Qt.ControlModifier),
        )

    def _best_markup_snap(self, raw_point: Point2D) -> SnapResult | None:
        if not self._snap_enabled or not self._markup_snap_candidates:
            return None
        threshold = self._snap_distance_threshold()
        if threshold is None:
            return None
        best: tuple[float, GuideSnapCandidate] | None = None
        for candidate in self._markup_snap_candidates:
            distance = math.hypot(
                candidate.point[0] - raw_point[0],
                candidate.point[1] - raw_point[1],
            )
            if distance > threshold:
                continue
            if best is None or distance < best[0]:
                best = (distance, candidate)
        if best is None:
            return None
        distance, candidate = best
        return SnapResult(
            point=candidate.point,
            mode=candidate.mode,
            distance=distance,
            segment_start=candidate.segment_start,
            segment_end=candidate.segment_end,
        )

    def _selection_entity_ids(
        self,
        start: Point2D,
        end: Point2D,
    ) -> set[str]:
        crossing = end[0] < start[0]
        return entities_in_rect(
            self._selectable_entities,
            SelectionRect.from_drag(start, end),
            crossing=crossing,
        )

    def _update_selection_rectangle(
        self,
        start: Point2D,
        end: Point2D,
    ) -> None:
        crossing = end[0] < start[0]
        self._selection_rect_mode = "cross" if crossing else "contain"
        color = "#4caf50" if crossing else "#2196f3"
        style = Qt.DashLine if crossing else Qt.SolidLine
        self._selection_rect_item.setPen(pg.mkPen(color, width=1.5, style=style))
        rect = SelectionRect.from_drag(start, end)
        self._selection_rect_item.setData(
            [rect.left, rect.right, rect.right, rect.left, rect.left],
            [rect.bottom, rect.bottom, rect.top, rect.top, rect.bottom],
        )

    def _clear_selection_interaction(self) -> None:
        self._select_press_scene_pos = None
        self._select_press_design_point = None
        self._select_press_entity_id = None
        self._select_dragging = False
        self._selection_rect_mode = None
        if self._plot is not None and hasattr(self, "_selection_rect_item"):
            self._selection_rect_item.setData([], [])

    def set_source_design_marks(self, points: list[Point2D | None]) -> None:
        self._source_design_marks = list(points[:2])
        while len(self._source_design_marks) < 2:
            self._source_design_marks.append(None)
        self._redraw_overlays()

    def set_alignment_draft_points(self, points: object) -> None:
        self._alignment_draft_points = [
            (float(point[0]), float(point[1]))
            for point in points
            if isinstance(point, (list, tuple)) and len(point) == 2
        ] if isinstance(points, (list, tuple)) else []
        self._redraw_alignment_draft()

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

        normalized = bool(enabled)
        if normalized != self._snap_enabled:
            self._markup_generation += 1
            self._pending_clicks.clear()
            self._pending_hover_markup.clear()
        self._snap_enabled = normalized
        if not self._snap_enabled:
            self._pending_hover_markup.clear()
            self._set_hover_snap(None)

    def focus_bounds(self) -> None:
        self.focus_gds_bounds()

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
        if self._pending_hover_scene_pos is not None and not self._hover_timer.isActive():
            self._hover_timer.start()

    def _redraw_overlays(self) -> None:
        if self._plot is None:
            return
        if self._document_preview_active:
            self._set_preview_overlay_visibility(False)
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
        self._redraw_mixed_preview()
        self._redraw_tool_measure()
        self._redraw_axis_triad()
        self._redraw_alignment_draft()

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

    def _redraw_alignment_draft(self) -> None:
        if self._plot is None or not hasattr(self, "_alignment_draft_item"):
            return
        for item in self._alignment_draft_label_items:
            self._plot.removeItem(item)
        self._alignment_draft_label_items.clear()
        if not self._alignment_draft_points:
            self._alignment_draft_item.setData([], [])
            return
        self._alignment_draft_item.setData(
            [point[0] for point in self._alignment_draft_points],
            [point[1] for point in self._alignment_draft_points],
        )
        for index, point in enumerate(self._alignment_draft_points, start=1):
            label = pg.TextItem(
                text=f"D{index}",
                color="#80deea",
                anchor=(0.0, 1.0),
                fill=pg.mkBrush(0, 0, 0, 150),
            )
            label.setPos(point[0], point[1])
            self._plot.addItem(label)
            self._alignment_draft_label_items.append(label)

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

        selected_points = [
            point
            for point in self._probe_route.points
            if point.enabled and route_entity_id(point.id) in self._selection.ids
        ]
        if not self._selection_managed and 0 <= self._selected_route_point_index < len(
            self._probe_route.points
        ):
            selected = self._probe_route.points[self._selected_route_point_index]
            if selected.enabled:
                selected_points = [selected]
        self._probe_route_selected_item.setData(
            [point.camera_center[0] for point in selected_points],
            [point.camera_center[1] for point in selected_points],
        )

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
        self._tool_sketch_point_item.setData([], [])
        self._tool_sketch_midpoint_item.setData([], [])
        self._tool_sketch_intersection_item.setData([], [])
        segments = list(self._tool_sketch_segments)
        if len(self._tool_sketch_points) == 2:
            segments.append((self._tool_sketch_points[0], self._tool_sketch_points[1]))
        if not segments and not self._tool_sketch_points:
            self._tool_sketch_item.setData([], [])
            self._markup_selected_item.setData([], [])
            return

        line_x: list[float] = []
        line_y: list[float] = []
        for start, end in segments:
            line_x.extend([start[0], end[0], float("nan")])
            line_y.extend([start[1], end[1], float("nan")])

        self._tool_sketch_item.setData(line_x, line_y)
        selected_segments = [
            entity.geometry
            for entity in self._selectable_entities
            if entity.id in self._selection.ids
            and entity.owner is EntityOwner.MARKUP
            and isinstance(entity.geometry, SegmentGeometry)
        ]
        selected_x: list[float] = []
        selected_y: list[float] = []
        for segment in selected_segments:
            selected_x.extend([segment.start[0], segment.end[0], float("nan")])
            selected_y.extend([segment.start[1], segment.end[1], float("nan")])
        self._markup_selected_item.setData(selected_x, selected_y)

    def _redraw_mixed_preview(self) -> None:
        if self._plot is None:
            return
        self._mixed_preview_point_item.setData(
            [point[0] for point in self._mixed_preview_points],
            [point[1] for point in self._mixed_preview_points],
        )
        line_x: list[float] = []
        line_y: list[float] = []
        for segment in self._mixed_preview_segments:
            line_x.extend([segment.start[0], segment.end[0], float("nan")])
            line_y.extend([segment.start[1], segment.end[1], float("nan")])
        self._mixed_preview_segment_item.setData(line_x, line_y)

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
        if self._probe_route is None or self._document_preview_active:
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

    def eventFilter(self, watched, event) -> bool:  # type: ignore[override]
        if (
            self._plot is None
            or watched is not self._plot.scene()
            or getattr(self, "_document_preview_active", False)
            or self._active_design_tool != "select"
            or self._document is None
        ):
            return super().eventFilter(watched, event)
        event_type = event.type()
        if event_type == QEvent.GraphicsSceneMousePress:
            if event.button() != Qt.LeftButton:
                return super().eventFilter(watched, event)
            position = event.scenePos()
            if not self._plot.sceneBoundingRect().contains(position):
                return super().eventFilter(watched, event)
            view_point = self._plot.getViewBox().mapSceneToView(position)
            design_point = (float(view_point.x()), float(view_point.y()))
            hit = self._hit_entity(design_point)
            self._select_press_scene_pos = QPointF(position)
            self._select_press_design_point = design_point
            self._select_press_entity_id = hit.id if hit is not None else None
            self._select_modifiers = event.modifiers()
            self._select_dragging = False
            event.accept()
            return True
        if event_type == QEvent.GraphicsSceneMouseMove:
            if self._select_press_scene_pos is None:
                return super().eventFilter(watched, event)
            if self._select_press_entity_id is not None:
                event.accept()
                return True
            position = event.scenePos()
            distance = (
                position - self._select_press_scene_pos
            ).manhattanLength()
            if distance >= QApplication.startDragDistance():
                self._select_dragging = True
            if self._select_dragging and self._select_press_design_point is not None:
                view_point = self._plot.getViewBox().mapSceneToView(position)
                current = (float(view_point.x()), float(view_point.y()))
                self._update_selection_rectangle(
                    self._select_press_design_point,
                    current,
                )
            event.accept()
            return True
        if event_type == QEvent.GraphicsSceneMouseRelease:
            if event.button() != Qt.LeftButton or self._select_press_scene_pos is None:
                return super().eventFilter(watched, event)
            position = event.scenePos()
            view_point = self._plot.getViewBox().mapSceneToView(position)
            current = (float(view_point.x()), float(view_point.y()))
            if self._select_dragging and self._select_press_design_point is not None:
                matched = self._selection_entity_ids(
                    self._select_press_design_point,
                    current,
                )
            elif self._select_press_entity_id is not None:
                matched = {self._select_press_entity_id}
            else:
                matched = set()
            mode = self._selection_update_mode(self._select_modifiers)
            self._clear_selection_interaction()
            self.entity_selection_requested.emit(matched, mode)
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def _on_mouse_clicked(self, event) -> None:  # pragma: no cover - UI interaction
        if (
            self._plot is None
            or self._document is None
            or getattr(self, "_document_preview_active", False)
        ):
            return
        is_left_click = event.button() == Qt.LeftButton
        is_double_click = self._is_double_click_event(event)
        modifiers = getattr(event, "modifiers", lambda: Qt.NoModifier)()
        shift_constraint, control_constraint = _DesignPlotPane._constraint_flags(
            modifiers
        )
        active_tool = getattr(self, "_active_design_tool", "legacy")
        if active_tool == "align" and not is_left_click:
            return
        alignment_click = active_tool == "align" and is_left_click
        route_pick = self._route_pick_mode is not None and is_left_click
        guide_click = (
            not route_pick
            and active_tool == "guide"
            and self._route_edit_enabled
            and is_left_click
        )
        route_click = (
            not route_pick
            and self._route_edit_enabled
            and is_left_click
            and active_tool in {"legacy", "point"}
        )
        select_click = active_tool == "select" and is_left_click
        if alignment_click or route_pick or route_click or guide_click or select_click:
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
        if alignment_click:
            action = "alignment_point"
            payload = ()
        elif route_pick:
            action = "route_pick"
            payload: tuple[object, ...] = (str(self._route_pick_mode),)
        elif guide_click:
            action = "guide_point"
            payload = ()
        elif route_click:
            action = (
                "point" if active_tool == "point" else "route_point"
            )
            payload = ()
        elif select_click:
            action = "select"
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
        if action == "select":
            self._emit_click_selection(raw_point, modifiers)
            return
        if bool(getattr(self._document, "file_backed", False)):
            self._submit_file_backed_click(
                action,
                raw_point,
                payload,
                modifiers=modifiers,
            )
            return
        started = perf_counter()
        snap_result = self._resolve_snap_result(raw_point)
        elapsed_ms = (perf_counter() - started) * 1000.0
        self._set_hover_snap(
            snap_result,
            shift=shift_constraint,
            control=control_constraint,
        )
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
                shift_constraint,
                control_constraint,
            )
            return
        if guide_click or route_click or alignment_click:
            self._execute_click_action(
                action,
                payload,
                snap_result,
                shift=shift_constraint,
                control=control_constraint,
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
        *,
        modifiers: Qt.KeyboardModifiers = Qt.NoModifier,
    ) -> None:
        shift_constraint, control_constraint = self._constraint_flags(modifiers)
        free_result = SnapResult(point=raw_point, mode="free", distance=0.0)
        if not self._snap_enabled:
            self._set_hover_snap(
                free_result,
                shift=shift_constraint,
                control=control_constraint,
            )
            self._execute_click_action(
                action,
                payload,
                free_result,
                shift=shift_constraint,
                control=control_constraint,
            )
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
            markup_result=self._best_markup_snap(raw_point),
            markup_generation=self._markup_generation,
            shift_constraint=shift_constraint,
            control_constraint=control_constraint,
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
        *,
        shift: bool = False,
        control: bool = False,
    ) -> None:
        x_value, y_value = snap_result.point
        if action == "guide_point" and self._guide_anchor is not None:
            x_value, y_value = constrain_vector_endpoint(
                self._guide_anchor,
                (x_value, y_value),
                shift=shift,
                control=control,
            )
        if action == "route_pick" and payload:
            self.route_pick_requested.emit(
                str(payload[0]),
                x_value,
                y_value,
                bool(shift),
                bool(control),
            )
        elif action == "route_point":
            self.route_point_requested.emit(x_value, y_value)
        elif action == "point":
            self.point_requested.emit(x_value, y_value)
        elif action == "alignment_point":
            self.alignment_point_requested.emit(x_value, y_value)
        elif action == "guide_point":
            self._accept_guide_point((x_value, y_value))
        elif action == "move":
            self.move_requested.emit(x_value, y_value)
        elif action == "calibration" and payload:
            self.calibration_point_selected.emit(int(payload[0]), x_value, y_value)

    def _accept_guide_point(self, point: Point2D) -> None:
        if not self._route_edit_enabled or self._active_design_tool != "guide":
            return
        if self._guide_anchor is None:
            self._guide_anchor = point
            self._tool_sketch_points = [point]
            self._redraw_overlays()
            return
        start = self._guide_anchor
        self._guide_anchor = None
        self._tool_sketch_points = []
        if start != point:
            self.guide_requested.emit(start, point)
        self._redraw_overlays()

    def _emit_click_selection(
        self,
        point: Point2D,
        modifiers: Qt.KeyboardModifiers,
    ) -> None:
        entity = self._hit_entity(point)
        matched = {entity.id} if entity is not None else set()
        self.entity_selection_requested.emit(
            matched,
            self._selection_update_mode(modifiers),
        )

    def _hit_entity(self, point: Point2D) -> SelectableDesignEntity | None:
        tolerance = self._snap_distance_threshold()
        if tolerance is None:
            return None
        hits = [
            entity
            for entity in self._selectable_entities
            if entity.hit_test(point, tolerance=tolerance)
        ]
        if not hits:
            return None
        return min(
            hits,
            key=lambda entity: 0 if entity.owner is EntityOwner.ROUTE else 1,
        )

    @staticmethod
    def _selection_update_mode(modifiers: Qt.KeyboardModifiers) -> str:
        if modifiers & Qt.ControlModifier:
            return "invert"
        if modifiers & Qt.ShiftModifier:
            return "add"
        return "replace"

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
        if (
            self._plot is None
            or self._document is None
            or getattr(self, "_document_preview_active", False)
        ):
            self._set_hover_snap(None)
            return
        position = self._pending_hover_scene_pos
        self._pending_hover_scene_pos = None
        if position is None or not self._plot.sceneBoundingRect().contains(position):
            self._set_hover_snap(None)
            return
        view_point = self._plot.getViewBox().mapSceneToView(position)
        raw_point = (float(view_point.x()), float(view_point.y()))
        modifiers = QApplication.keyboardModifiers()
        shift_constraint, control_constraint = self._constraint_flags(modifiers)
        if self._document.file_backed:
            self._submit_file_backed_hover(raw_point, modifiers=modifiers)
            return
        started = perf_counter()
        snap_result = self._resolve_snap_result(raw_point)
        elapsed_ms = (perf_counter() - started) * 1000.0
        self._set_hover_snap(
            snap_result,
            shift=shift_constraint,
            control=control_constraint,
        )
        self._log_hover_snap(raw_point, snap_result, elapsed_ms)

    def _submit_file_backed_hover(
        self,
        raw_point: Point2D,
        *,
        modifiers: Qt.KeyboardModifiers = Qt.NoModifier,
    ) -> None:
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
        shift_constraint, control_constraint = self._constraint_flags(modifiers)
        self._pending_hover_markup = {
            self._snap_request_id: (
                self._best_markup_snap(raw_point),
                shift_constraint,
                control_constraint,
            )
        }
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
            pending_hover = self._pending_hover_markup.pop(response.request_id, None)
            if pending_hover is None:
                markup_result = None
                shift_constraint = False
                control_constraint = False
            else:
                markup_result, shift_constraint, control_constraint = pending_hover
            result = self._nearest_screen_result(
                response.raw_point,
                response.result,
                markup_result,
            )
            self._set_hover_snap(
                result,
                shift=shift_constraint,
                control=control_constraint,
            )
            self._log_hover_snap(
                response.raw_point,
                result,
                response.elapsed_ms,
            )
            return
        if response.purpose != "click":
            return
        pending = self._pending_clicks.pop(response.request_id, None)
        if (
            pending is None
            or pending.config_generation != config.generation
            or pending.markup_generation != self._markup_generation
        ):
            return
        result = self._nearest_screen_result(
            pending.raw_point,
            response.result,
            pending.markup_result,
        )
        self._set_hover_snap(
            result,
            shift=pending.shift_constraint,
            control=pending.control_constraint,
        )
        logger.debug(
            "DESIGN SNAP click raw=(%.3f, %.3f) snapped=(%.3f, %.3f) mode=%s dist=%.4f elapsed_ms=%.2f",
            pending.raw_point[0],
            pending.raw_point[1],
            result.point[0],
            result.point[1],
            result.mode,
            result.distance,
            response.elapsed_ms,
        )
        self._execute_click_action(
            pending.action,
            pending.payload,
            result,
            shift=pending.shift_constraint,
            control=pending.control_constraint,
        )

    def _nearest_screen_result(
        self,
        raw_point: Point2D,
        *results: SnapResult | None,
    ) -> SnapResult:
        available = [result for result in results if result is not None]
        snapped = [result for result in available if result.mode != "free"]
        candidates = snapped or available
        if not candidates:
            return SnapResult(point=raw_point, mode="free", distance=0.0)
        return min(
            candidates,
            key=lambda result: self._screen_distance(raw_point, result.point),
        )

    def _screen_distance(self, first: Point2D, second: Point2D) -> float:
        if self._plot is not None:
            view_box = self._plot.getViewBox()
            try:
                first_scene = view_box.mapViewToScene(QPointF(*first))
                second_scene = view_box.mapViewToScene(QPointF(*second))
                return math.hypot(
                    second_scene.x() - first_scene.x(),
                    second_scene.y() - first_scene.y(),
                )
            except (AttributeError, RuntimeError, TypeError, ValueError):
                pass
        return math.hypot(second[0] - first[0], second[1] - first[1])

    def _resolve_snap_result(self, raw_point: Point2D) -> SnapResult:
        free_result = SnapResult(point=raw_point, mode="free", distance=0.0)
        if self._document is None or not self._snap_enabled:
            return free_result
        markup_result = self._best_markup_snap(raw_point)
        if self._document.file_backed or not self._document.has_snap_geometry():
            return self._nearest_screen_result(raw_point, free_result, markup_result)
        snap_threshold = self._snap_distance_threshold()
        if snap_threshold is None:
            return self._nearest_screen_result(raw_point, free_result, markup_result)
        snap_result = self._document.snap_point_info(
            raw_point,
            max_distance=snap_threshold,
        )
        if snap_result.distance > snap_threshold:
            snap_result = free_result
        return self._nearest_screen_result(raw_point, snap_result, markup_result)

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

    def _set_hover_snap(
        self,
        snap_result: SnapResult | None,
        *,
        shift: bool = False,
        control: bool = False,
    ) -> None:
        if snap_result is None:
            self._latest_hover_request_id = 0
        self._hover_snap = snap_result
        if self._active_design_tool == "guide" and self._guide_anchor is not None:
            if snap_result is None:
                self._tool_sketch_points = [self._guide_anchor]
            else:
                endpoint = constrain_vector_endpoint(
                    self._guide_anchor,
                    snap_result.point,
                    shift=shift,
                    control=control,
                )
                self._tool_sketch_points = [self._guide_anchor, endpoint]
            self._redraw_tool_sketch()
        self._redraw_hover()
        self.hover_snap_changed.emit(snap_result)
        self.tool_hover_snap_changed.emit(
            snap_result,
            bool(shift),
            bool(control),
        )

    def _redraw_hover(self) -> None:
        if self._plot is None or self._hover_snap is None:
            if self._plot is not None:
                self._hover_item.setData([], [])
                self._hover_segment_item.setData([], [])
            return
        point = self._hover_snap.point
        self._hover_item.setData([point[0]], [point[1]])
        if (
            self._hover_snap.mode in {
                "segment",
                "segment_center",
                "guide_end",
                "guide_center",
            }
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
        self._pending_hover_markup.clear()
        self._latest_hover_request_id = 0
        if hasattr(self, "_hover_timer"):
            self._hover_timer.stop()
        if self._route_geometry_redraw_timer is not None:
            self._route_geometry_redraw_timer.stop()
        self._stop_snap_worker(timeout_s=0.0)
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


