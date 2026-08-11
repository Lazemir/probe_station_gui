"""Route and route-preview rendering for the Design plot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QWidget

from probe_station_gui.design.model import Point2D
from probe_station_gui.design.navigation_geometry import route_arrow_segments
from probe_station_gui.design.selection_model import SelectionModel, route_entity_id
from probe_station_gui.route.model import MeasurementRoute

try:  # pragma: no cover - optional runtime dependency
    import pyqtgraph as pg
except ImportError:  # pragma: no cover - optional runtime dependency
    pg = None


@dataclass(frozen=True)
class RouteRenderPlan:
    route: MeasurementRoute | None = None
    selected_route_point_index: int = -1
    selection: SelectionModel = SelectionModel()
    selection_managed: bool = False
    preview_points: tuple[Point2D, ...] = ()
    preview_offsets: tuple[Point2D, ...] = ()
    document_preview_active: bool = False


@dataclass(frozen=True)
class RouteRenderState:
    route_point_count: int
    selected_centers: tuple[Point2D, ...]
    needle_one_count: int
    preview_point_count: int
    route_arrow_coordinate_count: int
    number_label_count: int


class DesignPlotRouteRenderer:
    """Own route scene items and deferred fixed-pixel geometry updates."""

    DETAIL_POINT_LIMIT = 300
    LABEL_POINT_LIMIT = 150

    def __init__(
        self,
        plot,
        *,
        pixel_size: Callable[[], float | None],
        visible: Callable[[], bool],
        parent: QWidget,
        _defer_scatter_add: bool = False,
    ) -> None:
        if pg is None:  # pragma: no cover - guarded by the pane
            raise RuntimeError("pyqtgraph is required for Design route rendering.")
        self._plot = plot
        self._pixel_size = pixel_size
        self._visible = visible
        self._plan = RouteRenderPlan()
        self._deferred = False
        self._scatter_added = False
        self._number_labels: list[object] = []
        self._route_point_count = 0
        self._selected_centers: tuple[Point2D, ...] = ()
        self._needle_one_count = 0
        self._preview_point_count = 0
        self._route_arrow_coordinate_count = 0

        self._route_item = plot.plot([], [], pen=pg.mkPen("#29b6f6", width=2.5))
        self._route_arrow_item = plot.plot(
            [], [], pen=pg.mkPen("#64b5f6", width=1.35)
        )
        self._needle_connector_item = plot.plot(
            [], [], pen=pg.mkPen("#cfd8dc", width=1, style=Qt.DotLine)
        )
        self._preview_item = plot.plot(
            [], [], pen=pg.mkPen("#ffca28", width=2, style=Qt.DashLine)
        )
        self._preview_arrow_item = plot.plot(
            [], [], pen=pg.mkPen("#64b5f6", width=1.35)
        )
        self._preview_needle_connector_item = plot.plot(
            [], [], pen=pg.mkPen("#ffe082", width=1, style=Qt.DotLine)
        )
        self._point_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#29b6f6", width=1.5),
            brush=pg.mkBrush(41, 182, 246, 170),
            size=9,
            symbol="o",
        )
        self._selected_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ff7043", width=2),
            brush=pg.mkBrush(255, 112, 67, 190),
            size=14,
            symbol="o",
        )
        self._needle_one_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ffd54f", width=1.5),
            brush=pg.mkBrush(255, 213, 79, 180),
            size=10,
            symbol="+",
        )
        self._needle_two_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ec407a", width=1.5),
            brush=pg.mkBrush(236, 64, 122, 170),
            size=10,
            symbol="x",
        )
        self._preview_point_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#ffca28", width=1.5),
            brush=pg.mkBrush(255, 202, 40, 100),
            size=8,
            symbol="o",
        )
        self._preview_needle_one_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#fff59d", width=1.2),
            brush=pg.mkBrush(255, 245, 157, 130),
            size=8,
            symbol="+",
        )
        self._preview_needle_two_item = pg.ScatterPlotItem(
            pen=pg.mkPen("#f48fb1", width=1.2),
            brush=pg.mkBrush(244, 143, 177, 120),
            size=8,
            symbol="x",
        )
        self._timer = QTimer(parent)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self.redraw_geometry)
        if not _defer_scatter_add:
            self._add_scatter_items()

    @property
    def state(self) -> RouteRenderState:
        return RouteRenderState(
            route_point_count=self._route_point_count,
            selected_centers=self._selected_centers,
            needle_one_count=self._needle_one_count,
            preview_point_count=self._preview_point_count,
            route_arrow_coordinate_count=self._route_arrow_coordinate_count,
            number_label_count=len(self._number_labels),
        )

    @property
    def pending_delay_ms(self) -> int | None:
        return self._timer.interval() if self._timer.isActive() else None

    def render(self, plan: RouteRenderPlan) -> None:
        self._plan = plan
        self._redraw_route()
        self._redraw_preview()

    def schedule(self) -> None:
        if self._deferred and self._timer.isActive():
            return
        self._deferred = True
        if not self._timer.isActive():
            self._timer.start(0)
        self._clear_arrows()

    def redraw_geometry(self) -> None:
        if self._plan.route is None and not self._plan.preview_points:
            self._deferred = False
            self._clear_arrows()
            return
        if self._pixel_size() is None:
            self._deferred = True
            self._clear_arrows()
            if self._visible():
                self._timer.start(16)
            return
        self._deferred = False
        self._redraw_route()
        self._redraw_preview()
        self._plot.update()

    def set_preview_visibility(self, visible: bool) -> None:
        for item in (*self._persistent_items(), *self._number_labels):
            item.setVisible(bool(visible))

    def shutdown(self) -> None:
        self._timer.stop()

    def _add_scatter_items(self) -> None:
        if self._scatter_added:
            return
        for item in self._scatter_items():
            self._plot.addItem(item)
        self._scatter_added = True

    def _redraw_route(self) -> None:
        route = self._plan.route
        if route is None or not route.points:
            self._clear_route()
            return
        points = [point for point in route.points if point.enabled]
        if not points:
            self._clear_route()
            return
        centers = [point.camera_center for point in points]
        self._route_item.setData(
            [point[0] for point in centers],
            [point[1] for point in centers],
        )
        self._point_item.setData(
            [point[0] for point in centers],
            [point[1] for point in centers],
        )
        arrow_x, arrow_y = self._arrow_segments(centers)
        self._route_arrow_item.setData(arrow_x, arrow_y)
        self._route_point_count = len(centers)
        self._route_arrow_coordinate_count = len(arrow_x)
        self._redraw_numbers()

        selected_points = [
            point
            for point in route.points
            if point.enabled and route_entity_id(point.id) in self._plan.selection.ids
        ]
        selected_index = self._plan.selected_route_point_index
        if (
            not self._plan.selection_managed
            and 0 <= selected_index < len(route.points)
        ):
            selected = route.points[selected_index]
            if selected.enabled:
                selected_points = [selected]
        self._selected_item.setData(
            [point.camera_center[0] for point in selected_points],
            [point.camera_center[1] for point in selected_points],
        )
        self._selected_centers = tuple(point.camera_center for point in selected_points)

        draw_details = len(points) <= self.DETAIL_POINT_LIMIT
        needle_one: list[Point2D] = []
        needle_two: list[Point2D] = []
        connector_x: list[float] = []
        connector_y: list[float] = []
        for route_index, point in enumerate(route.points):
            if not point.enabled:
                continue
            if not draw_details and route_index != selected_index:
                continue
            for offset_index, (_offset, hit) in enumerate(
                route.needle_hits_for_point(point)[:2]
            ):
                (needle_one if offset_index == 0 else needle_two).append(hit)
                connector_x.extend(
                    [point.camera_center[0], hit[0], float("nan")]
                )
                connector_y.extend(
                    [point.camera_center[1], hit[1], float("nan")]
                )
        self._needle_one_item.setData(
            [point[0] for point in needle_one],
            [point[1] for point in needle_one],
        )
        self._needle_two_item.setData(
            [point[0] for point in needle_two],
            [point[1] for point in needle_two],
        )
        self._needle_connector_item.setData(connector_x, connector_y)
        self._needle_one_count = len(needle_one)

    def _redraw_preview(self) -> None:
        points = self._plan.preview_points
        if not points:
            self._preview_item.setData([], [])
            self._preview_arrow_item.setData([], [])
            self._preview_point_item.setData([], [])
            self._preview_needle_one_item.setData([], [])
            self._preview_needle_two_item.setData([], [])
            self._preview_needle_connector_item.setData([], [])
            self._preview_point_count = 0
            return
        x_values = [point[0] for point in points]
        y_values = [point[1] for point in points]
        self._preview_item.setData(x_values, y_values)
        self._preview_point_item.setData(x_values, y_values)
        self._preview_arrow_item.setData(*self._arrow_segments(list(points)))
        needle_one: list[Point2D] = []
        needle_two: list[Point2D] = []
        connector_x: list[float] = []
        connector_y: list[float] = []
        for center in points:
            for offset_index, offset in enumerate(self._plan.preview_offsets[:2]):
                hit = (center[0] + offset[0], center[1] + offset[1])
                (needle_one if offset_index == 0 else needle_two).append(hit)
                connector_x.extend([center[0], hit[0], float("nan")])
                connector_y.extend([center[1], hit[1], float("nan")])
        self._preview_needle_one_item.setData(
            [point[0] for point in needle_one],
            [point[1] for point in needle_one],
        )
        self._preview_needle_two_item.setData(
            [point[0] for point in needle_two],
            [point[1] for point in needle_two],
        )
        self._preview_needle_connector_item.setData(connector_x, connector_y)
        self._preview_point_count = len(points)

    def _redraw_numbers(self) -> None:
        self._clear_numbers()
        route = self._plan.route
        if route is None or self._plan.document_preview_active:
            return
        enabled_count = sum(point.enabled for point in route.points)
        draw_labels = enabled_count <= self.LABEL_POINT_LIMIT
        for route_index, route_point in enumerate(route.points):
            if not route_point.enabled:
                continue
            if (
                not draw_labels
                and route_index != self._plan.selected_route_point_index
            ):
                continue
            item = pg.TextItem(
                text=str(route_index + 1),
                color="#e1f5fe",
                anchor=(0.0, 1.0),
                fill=pg.mkBrush(0, 0, 0, 130),
            )
            item.setPos(route_point.camera_center[0], route_point.camera_center[1])
            self._plot.addItem(item)
            self._number_labels.append(item)

    def _clear_route(self) -> None:
        for item in (
            self._route_item,
            self._route_arrow_item,
            self._point_item,
            self._selected_item,
            self._needle_one_item,
            self._needle_two_item,
            self._needle_connector_item,
        ):
            item.setData([], [])
        self._route_point_count = 0
        self._selected_centers = ()
        self._needle_one_count = 0
        self._route_arrow_coordinate_count = 0
        self._clear_numbers()

    def _clear_numbers(self) -> None:
        for item in self._number_labels:
            self._plot.removeItem(item)
        self._number_labels.clear()

    def _clear_arrows(self) -> None:
        self._route_arrow_item.setData([], [])
        self._preview_arrow_item.setData([], [])
        self._route_arrow_coordinate_count = 0

    def _arrow_segments(
        self,
        centers: list[Point2D],
    ) -> tuple[list[float], list[float]]:
        return route_arrow_segments(
            centers,
            pixel_size=self._pixel_size(),
            deferred=self._deferred,
        )

    def _persistent_items(self) -> tuple[object, ...]:
        return (
            self._route_item,
            self._route_arrow_item,
            self._needle_connector_item,
            self._preview_item,
            self._preview_arrow_item,
            self._preview_needle_connector_item,
            *self._scatter_items(),
        )

    def _scatter_items(self) -> tuple[object, ...]:
        return (
            self._point_item,
            self._selected_item,
            self._needle_one_item,
            self._needle_two_item,
            self._preview_point_item,
            self._preview_needle_one_item,
            self._preview_needle_two_item,
        )

__all__ = [
    "DesignPlotRouteRenderer",
    "RouteRenderPlan",
    "RouteRenderState",
]
