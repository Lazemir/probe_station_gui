"""Non-raster pyqtgraph rendering for the Design plot."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from time import perf_counter

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget

from probe_station_gui.design.model import LayerKey, Point2D, SnapResult
from probe_station_gui.design.plot_interaction import (
    GuidePreview,
    SelectionClick,
    SelectionPreview,
)
from probe_station_gui.design.plot_presentation import (
    DirtyRegion,
    PlotPresentation,
    PlotRenderPlan,
    PresentationChange,
)
from probe_station_gui.design.selection_geometry import SegmentGeometry, SelectionRect
from probe_station_gui.design.selection_model import EntityOwner
from probe_station_gui.views.design_plot_route_rendering import (
    DesignPlotRouteRenderer,
    RouteRenderPlan,
)
from probe_station_gui.views.design_plot_viewport import DesignPlotViewport

try:  # pragma: no cover - optional runtime dependency
    import pyqtgraph as pg
except ImportError:  # pragma: no cover - optional runtime dependency
    pg = None


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlotRenderState:
    target_points: tuple[Point2D, ...]
    selected_target_points: tuple[Point2D, ...]
    current_crosshair_coordinate_count: int
    hover_segment: tuple[Point2D, ...]
    overlays_visible: bool
    layer_item_count: int
    selected_route_centers: tuple[Point2D, ...]
    selected_markup_segments: tuple[tuple[Point2D, Point2D], ...]
    selection_rectangle_mode: str | None
    selection_rectangle_color: str | None
    focus_candidate_outline: tuple[Point2D, ...]
    selected_focus_points: tuple[Point2D, ...]
    sketch_candidate_count: int


class DesignPlotRenderer:
    """Render every dynamic non-raster item behind one plan interface."""

    CURRENT_CROSSHAIR_HALF_SIZE_PX = 8.0

    def __init__(
        self,
        plot,
        viewport: DesignPlotViewport,
        *,
        parent: QWidget,
    ) -> None:
        if pg is None:  # pragma: no cover - guarded by the pane
            raise RuntimeError("pyqtgraph is required for Design plot rendering.")
        self._plot = plot
        self._viewport = viewport
        self._plan = PlotRenderPlan()
        self._overlays_visible = True
        self._layer_items: list[object] = []
        self._tool_measure_label_items: list[object] = []
        self._alignment_draft_label_items: list[object] = []
        self._target_points_state: tuple[Point2D, ...] = ()
        self._selected_target_points_state: tuple[Point2D, ...] = ()
        self._current_crosshair_coordinate_count = 0
        self._hover_segment_state: tuple[Point2D, ...] = ()
        self._selected_markup_segments_state: tuple[
            tuple[Point2D, Point2D], ...
        ] = ()
        self._selection_rectangle_mode: str | None = None
        self._selection_rectangle_color: str | None = None
        self._focus_candidate_outline: tuple[Point2D, ...] = ()

        self._target_route_item = plot.plot(
            [], [], pen=pg.mkPen("#4dd0e1", width=2)
        )
        self._route = DesignPlotRouteRenderer(
            plot,
            pixel_size=viewport.visible_pixel_size,
            visible=parent.isVisible,
            parent=parent,
            _defer_scatter_add=True,
        )
        self._hover_segment_item = plot.plot(
            [], [], pen=pg.mkPen("#ffffff", width=2)
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
        self._tool_measure_item = plot.plot(
            [], [], pen=pg.mkPen("#ffffff", width=1.5, style=Qt.DashLine)
        )
        self._tool_measure_point_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ffffff", width=1.5),
            brush=pg.mkBrush(255, 255, 255, 80),
            size=8,
            symbol="o",
        )
        self._tool_sketch_item = plot.plot(
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
        self._markup_selected_item = plot.plot(
            [], [], pen=pg.mkPen("#ff7043", width=3.0, style=Qt.DashLine)
        )
        self._mixed_preview_segment_item = plot.plot(
            [], [], pen=pg.mkPen("#ffca28", width=2.0, style=Qt.DashLine)
        )
        self._mixed_preview_point_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ffca28", width=1.5),
            brush=pg.mkBrush(255, 202, 40, 100),
            size=9,
            symbol="o",
        )
        self._selection_rect_item = plot.plot([], [])
        self._axis_triad_x_item = plot.plot(
            [], [], pen=pg.mkPen("#ef5350", width=2.2)
        )
        self._axis_triad_y_item = plot.plot(
            [], [], pen=pg.mkPen("#66bb6a", width=2.2)
        )
        self._axis_triad_z_item = plot.plot(
            [], [], pen=pg.mkPen("#42a5f5", width=2.2)
        )
        self._axis_triad_z_dot_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#42a5f5", width=1.0),
            brush=pg.mkBrush("#42a5f5"),
            size=5.0,
            symbol="o",
        )
        self._axis_triad_x_label = pg.TextItem(
            text="X", color="#ef5350", anchor=(0.0, 0.5)
        )
        self._axis_triad_y_label = pg.TextItem(
            text="Y", color="#66bb6a", anchor=(0.5, 1.0)
        )
        self._axis_triad_z_label = pg.TextItem(
            text="Z", color="#42a5f5", anchor=(1.0, 1.0)
        )
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
        self._current_crosshair_item = plot.plot(
            [], [], pen=pg.mkPen("#81c784", width=1.5)
        )
        self._source_mark_one_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ffd54f", width=2),
            brush=pg.mkBrush(255, 213, 79, 180),
            size=12,
            symbol="o",
        )
        self._source_mark_two_item = pg.ScatterPlotItem(
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
        self._fov_item = plot.plot([], [], pen=pg.mkPen("#81c784", width=1))
        self._focus_candidate_item = plot.plot(
            [], [], pen=pg.mkPen("#ffee58", width=2, style=Qt.DashLine)
        )
        self._selected_focus_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ffca28", width=2),
            brush=pg.mkBrush(255, 202, 40, 100),
            size=14,
            symbol="+",
        )
        self._add_scatter_items_in_stack_order()
        self._viewport.view_box.sigRangeChanged.connect(self._on_view_range_changed)

    @property
    def state(self) -> PlotRenderState:
        return PlotRenderState(
            target_points=self._target_points_state,
            selected_target_points=self._selected_target_points_state,
            current_crosshair_coordinate_count=(
                self._current_crosshair_coordinate_count
            ),
            hover_segment=self._hover_segment_state,
            overlays_visible=self._overlays_visible,
            layer_item_count=len(self._layer_items),
            selected_route_centers=self._route.state.selected_centers,
            selected_markup_segments=self._selected_markup_segments_state,
            selection_rectangle_mode=self._selection_rectangle_mode,
            selection_rectangle_color=self._selection_rectangle_color,
            focus_candidate_outline=self._focus_candidate_outline,
            selected_focus_points=(
                ()
                if self._plan.selected_focus_point is None
                else (self._plan.selected_focus_point,)
            ),
            sketch_candidate_count=0,
        )

    def render(self, change: PresentationChange) -> None:
        self._plan = change.plan
        if change.dirty & DirtyRegion.DOCUMENT:
            self._redraw_document()
        if change.route_geometry_changed:
            self._route.schedule()
        if change.overlay_refresh:
            self._redraw_overlays()
            return
        self._redraw_dirty(change.dirty)

    def apply(self, mutation, *args, **kwargs) -> PresentationChange:
        """Apply one presentation mutation and render its normalized change."""
        change = mutation(*args, **kwargs)
        self.render(change)
        return change

    def apply_navigation(
        self,
        presentation: PlotPresentation,
        mutation,
        *args,
        **kwargs,
    ) -> PresentationChange:
        change = mutation(*args, **kwargs)
        self._viewport.apply_presentation(presentation, recompute=True)
        self.render(change)
        return change

    def apply_interaction_effect(
        self,
        effect: object,
        presentation: PlotPresentation,
    ) -> bool:
        if isinstance(effect, GuidePreview):
            self.apply(presentation.set_guide_preview, effect.points)
        elif isinstance(effect, SelectionClick):
            self.apply(presentation.set_selected_focus_point, effect.point)
        elif isinstance(effect, SelectionPreview):
            self.set_selection_preview(effect)
        else:
            return False
        return True

    def set_hover(self, snap_result: SnapResult | None) -> None:
        self._redraw_hover(snap_result)

    def set_selection_preview(self, preview: SelectionPreview) -> None:
        if preview.start is None or preview.end is None:
            self._selection_rect_item.setData([], [])
            self._selection_rectangle_mode = None
            self._selection_rectangle_color = None
            return
        color = "#4caf50" if preview.crossing else "#2196f3"
        style = Qt.DashLine if preview.crossing else Qt.SolidLine
        self._selection_rect_item.setPen(pg.mkPen(color, width=1.5, style=style))
        rect = SelectionRect.from_drag(preview.start, preview.end)
        self._selection_rect_item.setData(
            [rect.left, rect.right, rect.right, rect.left, rect.left],
            [rect.bottom, rect.bottom, rect.top, rect.top, rect.bottom],
        )
        self._selection_rectangle_mode = "cross" if preview.crossing else "contain"
        self._selection_rectangle_color = color

    def set_preview_visibility(self, visible: bool) -> None:
        self._overlays_visible = bool(visible)
        for item in (
            *self._persistent_overlay_items(),
            *self._tool_measure_label_items,
            *self._alignment_draft_label_items,
        ):
            item.setVisible(self._overlays_visible)
        self._route.set_preview_visibility(self._overlays_visible)

    def view_changed(self) -> None:
        self._route.schedule()
        self._redraw_axis_triad()
        self._redraw_current_position()

    def _on_view_range_changed(self, *_unused) -> None:
        self.view_changed()

    def shown(self) -> None:
        self._route.schedule()

    def shutdown(self) -> None:
        self._route.shutdown()

    def _redraw_dirty(self, dirty: DirtyRegion) -> None:
        if dirty & DirtyRegion.MIXED_PREVIEW:
            self._redraw_mixed_preview()
        if dirty & DirtyRegion.ALIGNMENT:
            self._redraw_alignment_draft()
        if dirty & DirtyRegion.CURRENT:
            self._redraw_current_position()
        if dirty & DirtyRegion.FOCUS:
            self._redraw_focus_reference()
        if dirty & DirtyRegion.TOOL_SKETCH:
            self._redraw_tool_sketch()
        if dirty & DirtyRegion.HOVER:
            self._redraw_hover(self._plan.hover_snap)

    def _redraw_document(self) -> None:
        started = perf_counter()
        for item in self._layer_items:
            self._plot.removeItem(item)
        self._layer_items.clear()
        document = self._plan.document
        if document is None:
            return
        if document.file_backed:
            return
        point_count = 0
        for layer_key, (x_data, y_data) in document.visible_plot_paths().items():
            if len(x_data) == 0:
                continue
            line = self._plot.plot(
                x_data,
                y_data,
                pen=pg.mkPen(_layer_color(layer_key), width=1),
            )
            self._layer_items.append(line)
            point_count += len(x_data)
        logger.debug(
            "DESIGN RENDER full items=%d points=%d elapsed_ms=%.2f",
            len(self._layer_items),
            point_count,
            (perf_counter() - started) * 1000.0,
        )

    def _redraw_overlays(self) -> None:
        if self._plan.preview_active:
            self.set_preview_visibility(False)
            return
        targets = self._plan.targets
        target_points = tuple(target.design_center for target in targets)
        self._target_points_state = target_points
        self._target_item.setData(
            [point[0] for point in target_points],
            [point[1] for point in target_points],
        )
        self._target_route_item.setData(
            [point[0] for point in target_points],
            [point[1] for point in target_points],
        )
        self._route.render(self._route_plan())
        self._redraw_tool_sketch()
        self._redraw_mixed_preview()
        self._redraw_tool_measure()
        self._redraw_axis_triad()
        self._redraw_alignment_draft()
        self._redraw_focus_reference()

        selected = next(
            (
                target
                for target in targets
                if target.id == self._plan.selected_target_id
            ),
            None,
        )
        self._selected_target_points_state = (
            () if selected is None else (selected.design_center,)
        )
        _set_points(self._selected_target_item, self._selected_target_points_state)
        self._redraw_current_position()
        _set_slot_data(self._source_mark_one_item, self._plan.source_design_marks[0])
        _set_slot_data(self._source_mark_two_item, self._plan.source_design_marks[1])
        _set_points(self._check_mark_item, self._plan.check_design_marks)
        self._redraw_hover(self._plan.hover_snap)

    def _redraw_focus_reference(self) -> None:
        candidate = self._plan.focus_candidate
        if candidate is None:
            self._focus_candidate_item.setData([], [])
            self._focus_candidate_outline = ()
        else:
            left, bottom, right, top = candidate.bounds
            self._focus_candidate_outline = (
                (left, bottom),
                (right, bottom),
                (right, top),
                (left, top),
                (left, bottom),
            )
            self._focus_candidate_item.setData(
                [left, right, right, left, left],
                [bottom, bottom, top, top, bottom],
            )
        _set_slot_data(self._selected_focus_item, self._plan.selected_focus_point)

    def _redraw_alignment_draft(self) -> None:
        for item in self._alignment_draft_label_items:
            self._plot.removeItem(item)
        self._alignment_draft_label_items.clear()
        points = self._plan.alignment_draft_points
        _set_points(self._alignment_draft_item, points)
        for index, point in enumerate(points, start=1):
            label = pg.TextItem(
                text=f"D{index}",
                color="#80deea",
                anchor=(0.0, 1.0),
                fill=pg.mkBrush(0, 0, 0, 150),
            )
            label.setPos(point[0], point[1])
            self._plot.addItem(label)
            self._alignment_draft_label_items.append(label)

    def _redraw_current_position(self) -> None:
        point = self._plan.current_design_position
        if point is None:
            self._current_crosshair_item.setData([], [])
            self._current_item.setData([], [])
            self._fov_item.setData([], [])
            self._current_crosshair_coordinate_count = 0
            return
        pixel_size = self._viewport.data_units_per_screen_pixel()
        if self._plan.document is None or pixel_size is None:
            self._current_crosshair_item.setData([], [])
            self._current_crosshair_coordinate_count = 0
        else:
            half_size = self.CURRENT_CROSSHAIR_HALF_SIZE_PX * pixel_size
            self._current_crosshair_item.setData(
                [
                    point[0] - half_size,
                    point[0] + half_size,
                    float("nan"),
                    point[0],
                    point[0],
                ],
                [
                    point[1],
                    point[1],
                    float("nan"),
                    point[1] - half_size,
                    point[1] + half_size,
                ],
            )
            self._current_crosshair_coordinate_count = 5
        _set_slot_data(self._current_item, point)
        if self._plan.fov_design_size is None:
            self._fov_item.setData([], [])
            return
        half_w = abs(float(self._plan.fov_design_size[0])) * 0.5
        half_h = abs(float(self._plan.fov_design_size[1])) * 0.5
        self._fov_item.setData(
            [
                point[0] - half_w,
                point[0] + half_w,
                point[0] + half_w,
                point[0] - half_w,
                point[0] - half_w,
            ],
            [
                point[1] - half_h,
                point[1] - half_h,
                point[1] + half_h,
                point[1] + half_h,
                point[1] - half_h,
            ],
        )

    def _redraw_tool_sketch(self) -> None:
        self._tool_sketch_point_item.setData([], [])
        self._tool_sketch_midpoint_item.setData([], [])
        self._tool_sketch_intersection_item.setData([], [])
        segments = list(self._plan.tool_sketch_segments)
        if len(self._plan.tool_sketch_points) == 2:
            segments.append(
                (self._plan.tool_sketch_points[0], self._plan.tool_sketch_points[1])
            )
        if not segments and not self._plan.tool_sketch_points:
            self._tool_sketch_item.setData([], [])
            self._markup_selected_item.setData([], [])
            return
        line_x, line_y = _segment_lines(segments)
        self._tool_sketch_item.setData(line_x, line_y)
        selected_segments = [
            entity.geometry
            for entity in self._plan.selectable_entities
            if entity.id in self._plan.selection.ids
            and entity.owner is EntityOwner.MARKUP
            and isinstance(entity.geometry, SegmentGeometry)
        ]
        self._selected_markup_segments_state = tuple(
            (segment.start, segment.end) for segment in selected_segments
        )
        selected_x, selected_y = _segment_lines(
            [(segment.start, segment.end) for segment in selected_segments]
        )
        self._markup_selected_item.setData(selected_x, selected_y)

    def _redraw_mixed_preview(self) -> None:
        _set_points(self._mixed_preview_point_item, self._plan.mixed_preview_points)
        line_x, line_y = _segment_lines(
            [
                (segment.start, segment.end)
                for segment in self._plan.mixed_preview_segments
            ]
        )
        self._mixed_preview_segment_item.setData(line_x, line_y)

    def _redraw_tool_measure(self) -> None:
        self._clear_tool_measure_labels()
        segments = list(self._plan.tool_measure_segments)
        if len(self._plan.tool_measure_points) == 2:
            segments.append(
                (self._plan.tool_measure_points[0], self._plan.tool_measure_points[1])
            )
        if not segments and not self._plan.tool_measure_points:
            self._tool_measure_item.setData([], [])
            self._tool_measure_point_item.setData([], [])
            return
        line_x, line_y = _segment_lines(segments)
        endpoints: list[Point2D] = []
        for start, end in segments:
            endpoints.extend((start, end))
            self._add_tool_measure_labels(start, end)
        if len(self._plan.tool_measure_points) == 1:
            endpoints.append(self._plan.tool_measure_points[0])
        self._tool_measure_item.setData(line_x, line_y)
        _set_points(self._tool_measure_point_item, endpoints)

    def _clear_tool_measure_labels(self) -> None:
        for item in self._tool_measure_label_items:
            self._plot.removeItem(item)
        self._tool_measure_label_items.clear()

    def _add_tool_measure_labels(self, start: Point2D, end: Point2D) -> None:
        dx = float(end[0]) - float(start[0])
        dy = float(end[1]) - float(start[1])
        length = math.hypot(dx, dy)
        if length <= 1e-12:
            return
        midpoint_x = (float(start[0]) + float(end[0])) * 0.5
        midpoint_y = (float(start[1]) + float(end[1])) * 0.5
        pixel_size = self._viewport.data_units_per_screen_pixel() or 1.0
        normal_x = -dy / length
        normal_y = dx / length
        angle = -math.degrees(math.atan2(dy, dx))
        for text, color, offset_px in (
            (f"D={length:.3f}", "#fff59d", 18.0),
            (f"X={dx:.3f}", "#ef5350", 34.0),
            (f"Y={dy:.3f}", "#66bb6a", 50.0),
        ):
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
        labels = (
            self._axis_triad_x_label,
            self._axis_triad_y_label,
            self._axis_triad_z_label,
        )
        if self._plan.document is None:
            self._axis_triad_x_item.setData([], [])
            self._axis_triad_y_item.setData([], [])
            self._axis_triad_z_item.setData([], [])
            self._axis_triad_z_dot_item.setData([], [])
            for item in labels:
                item.setVisible(False)
            return
        pixel_size = self._viewport.data_units_per_screen_pixel()
        if pixel_size is None:
            return
        view_range = self._plot.getViewBox().viewRange()
        if not isinstance(view_range, list) or len(view_range) < 2:
            return
        x_min = float(view_range[0][0])
        y_min = float(view_range[1][0])
        margin = pixel_size * 34.0
        length = pixel_size * 42.0
        base = (x_min + margin, y_min + margin)
        x_end = (base[0] + length, base[1])
        y_end = (base[0], base[1] + length)
        arrow = pixel_size * 7.0
        z_radius = pixel_size * 7.0
        circle = tuple(
            (
                base[0] + math.cos(index / 32.0 * math.tau) * z_radius,
                base[1] + math.sin(index / 32.0 * math.tau) * z_radius,
            )
            for index in range(33)
        )
        self._axis_triad_x_item.setData(
            [
                base[0], x_end[0], float("nan"), x_end[0], x_end[0] - arrow,
                float("nan"), x_end[0], x_end[0] - arrow,
            ],
            [
                base[1], x_end[1], float("nan"), x_end[1],
                x_end[1] + arrow * 0.55, float("nan"), x_end[1],
                x_end[1] - arrow * 0.55,
            ],
        )
        self._axis_triad_y_item.setData(
            [
                base[0], y_end[0], float("nan"), y_end[0],
                y_end[0] - arrow * 0.55, float("nan"), y_end[0],
                y_end[0] + arrow * 0.55,
            ],
            [
                base[1], y_end[1], float("nan"), y_end[1], y_end[1] - arrow,
                float("nan"), y_end[1], y_end[1] - arrow,
            ],
        )
        self._axis_triad_z_item.setData(
            [point[0] for point in circle],
            [point[1] for point in circle],
        )
        self._axis_triad_z_dot_item.setData([base[0]], [base[1]])
        self._axis_triad_x_label.setPos(x_end[0] + pixel_size * 5.0, x_end[1])
        self._axis_triad_y_label.setPos(y_end[0], y_end[1] + pixel_size * 5.0)
        self._axis_triad_z_label.setPos(
            base[0] - pixel_size * 10.0,
            base[1] + pixel_size * 16.0,
        )
        for item in labels:
            item.setVisible(True)

    def _redraw_hover(self, snap_result: SnapResult | None) -> None:
        if snap_result is None:
            self._hover_item.setData([], [])
            self._hover_segment_item.setData([], [])
            self._hover_segment_state = ()
            return
        _set_slot_data(self._hover_item, snap_result.point)
        if (
            snap_result.mode
            in {"segment", "segment_center", "guide_end", "guide_center"}
            and snap_result.segment_start is not None
            and snap_result.segment_end is not None
        ):
            self._hover_segment_state = (
                snap_result.segment_start,
                snap_result.segment_end,
            )
            self._hover_segment_item.setData(
                [snap_result.segment_start[0], snap_result.segment_end[0]],
                [snap_result.segment_start[1], snap_result.segment_end[1]],
            )
        else:
            self._hover_segment_item.setData([], [])
            self._hover_segment_state = ()

    def _route_plan(self) -> RouteRenderPlan:
        return RouteRenderPlan(
            route=self._plan.probe_route,
            selected_route_point_index=self._plan.selected_route_point_index,
            selection=self._plan.selection,
            selection_managed=self._plan.selection_managed,
            preview_points=self._plan.probe_route_preview_points,
            preview_offsets=self._plan.probe_route_preview_offsets,
            document_preview_active=self._plan.preview_active,
        )

    def _persistent_overlay_items(self) -> tuple[object, ...]:
        return (
            self._target_route_item,
            self._hover_segment_item,
            self._hover_item,
            self._target_item,
            self._tool_measure_item,
            self._tool_measure_point_item,
            self._tool_sketch_item,
            self._tool_sketch_point_item,
            self._tool_sketch_midpoint_item,
            self._tool_sketch_intersection_item,
            self._markup_selected_item,
            self._mixed_preview_segment_item,
            self._mixed_preview_point_item,
            self._selection_rect_item,
            self._axis_triad_x_item,
            self._axis_triad_y_item,
            self._axis_triad_z_item,
            self._axis_triad_z_dot_item,
            self._axis_triad_x_label,
            self._axis_triad_y_label,
            self._axis_triad_z_label,
            self._selected_target_item,
            self._current_item,
            self._current_crosshair_item,
            self._source_mark_one_item,
            self._source_mark_two_item,
            self._alignment_draft_item,
            self._check_mark_item,
            self._fov_item,
            self._focus_candidate_item,
            self._selected_focus_item,
        )

    def _add_scatter_items_in_stack_order(self) -> None:
        self._plot.addItem(self._hover_item)
        self._plot.addItem(self._target_item)
        self._route._add_scatter_items()
        for item in (
            self._tool_measure_point_item,
            self._tool_sketch_point_item,
            self._tool_sketch_midpoint_item,
            self._tool_sketch_intersection_item,
            self._mixed_preview_point_item,
            self._axis_triad_x_label,
            self._axis_triad_y_label,
            self._axis_triad_z_label,
            self._axis_triad_z_dot_item,
            self._selected_target_item,
            self._current_item,
            self._source_mark_one_item,
            self._source_mark_two_item,
            self._alignment_draft_item,
            self._check_mark_item,
            self._selected_focus_item,
        ):
            self._plot.addItem(item)


def _set_slot_data(item, point: Point2D | None) -> None:
    if point is None:
        item.setData([], [])
    else:
        item.setData([point[0]], [point[1]])


def _set_points(item, points) -> None:
    item.setData(
        [point[0] for point in points],
        [point[1] for point in points],
    )


def _segment_lines(
    segments: list[tuple[Point2D, Point2D]],
) -> tuple[list[float], list[float]]:
    x_values: list[float] = []
    y_values: list[float] = []
    for start, end in segments:
        x_values.extend([start[0], end[0], float("nan")])
        y_values.extend([start[1], end[1], float("nan")])
    return x_values, y_values


def _layer_color(layer_key: LayerKey) -> QColor:
    hue = int((layer_key[0] * 57 + layer_key[1] * 19) % 360)
    return QColor.fromHsv(hue, 180, 210, 180)


__all__ = ["DesignPlotRenderer", "PlotRenderState"]
