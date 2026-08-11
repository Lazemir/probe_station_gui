from __future__ import annotations

# ruff: noqa: E402

import os
from pathlib import Path

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pg = pytest.importorskip("pyqtgraph")

from PySide6.QtWidgets import QApplication

from probe_station_gui.design.model import DesignDocument
from probe_station_gui.design.selection_model import SelectionModel, route_entity_id
from probe_station_gui.route.model import MeasurementRoute, NeedleOffset, RoutePoint
from probe_station_gui.views.design_plot_route_rendering import (
    DesignPlotRouteRenderer,
    RouteRenderPlan,
)


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _route(tmp_path: Path, count: int = 3) -> MeasurementRoute:
    (tmp_path / "chip.gds").write_bytes(b"gds")
    document = DesignDocument(
        path=tmp_path / "chip.gds",
        library=None,
        top_cell=None,
        top_cell_name="TOP",
        cell_names=("TOP",),
        dbu=1e-6,
        user_unit=1e-9,
        bounds=(0.0, 0.0, 100.0, 50.0),
        polygons_by_layer={},
        visible_layers=frozenset(),
    )
    route = MeasurementRoute.default_for_document(document)
    route.needle_offsets = [
        NeedleOffset("n1", "N1", 1.0, 0.0),
        NeedleOffset("n2", "N2", -1.0, 0.0),
    ]
    route.points = [
        RoutePoint(f"p{index}", f"P{index}", (float(index * 10), 0.0))
        for index in range(count)
    ]
    return route


def test_route_renderer_draws_lines_selection_needles_and_preview(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    plot = pg.PlotWidget()
    route = _route(tmp_path)
    renderer = DesignPlotRouteRenderer(
        plot,
        pixel_size=lambda: 1.0,
        visible=lambda: True,
        parent=plot,
    )
    plan = RouteRenderPlan(
        route=route,
        selected_route_point_index=-1,
        selection=SelectionModel(frozenset({route_entity_id("p1")})),
        selection_managed=True,
        preview_points=((0.0, 5.0), (10.0, 5.0)),
        preview_offsets=((1.0, 0.0), (-1.0, 0.0)),
    )

    renderer.render(plan)

    assert renderer.state.route_point_count == 3
    assert renderer.state.selected_centers == ((10.0, 0.0),)
    assert renderer.state.needle_one_count == 3
    assert renderer.state.preview_point_count == 2
    renderer.shutdown()
    plot.deleteLater()


def test_schedule_clears_arrows_then_coalesces_zero_delay_redraw(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    plot = pg.PlotWidget()
    renderer = DesignPlotRouteRenderer(
        plot,
        pixel_size=lambda: 1.0,
        visible=lambda: True,
        parent=plot,
    )
    renderer.render(RouteRenderPlan(route=_route(tmp_path)))
    renderer.redraw_geometry()
    assert renderer.state.route_arrow_coordinate_count > 0

    renderer.schedule()

    assert renderer.state.route_arrow_coordinate_count == 0
    assert renderer.pending_delay_ms == 0
    renderer.shutdown()
    plot.deleteLater()


def test_missing_scale_retries_only_while_visible(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    plot = pg.PlotWidget()
    visible_renderer = DesignPlotRouteRenderer(
        plot,
        pixel_size=lambda: None,
        visible=lambda: True,
        parent=plot,
    )
    visible_renderer.render(RouteRenderPlan(route=_route(tmp_path)))

    visible_renderer.redraw_geometry()
    assert visible_renderer.pending_delay_ms == 16
    visible_renderer.shutdown()

    hidden_renderer = DesignPlotRouteRenderer(
        plot,
        pixel_size=lambda: None,
        visible=lambda: False,
        parent=plot,
    )
    hidden_renderer.render(RouteRenderPlan(route=_route(tmp_path)))
    hidden_renderer.redraw_geometry()
    assert hidden_renderer.pending_delay_ms is None
    hidden_renderer.shutdown()
    plot.deleteLater()


def test_dynamic_route_labels_are_removed_before_tracking_is_cleared(
    qt_app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plot = pg.PlotWidget()
    renderer = DesignPlotRouteRenderer(
        plot,
        pixel_size=lambda: 1.0,
        visible=lambda: True,
        parent=plot,
    )
    renderer.render(RouteRenderPlan(route=_route(tmp_path)))
    labels = tuple(
        item for item in plot.plotItem.items if isinstance(item, pg.TextItem)
    )
    removed: list[object] = []
    real_remove = plot.removeItem

    def remove(item) -> None:
        removed.append(item)
        real_remove(item)

    monkeypatch.setattr(plot, "removeItem", remove)

    renderer.render(RouteRenderPlan())

    assert labels
    assert removed[: len(labels)] == list(labels)
    assert renderer.state.number_label_count == 0
    renderer.shutdown()
    plot.deleteLater()
