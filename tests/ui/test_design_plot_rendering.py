from __future__ import annotations

# ruff: noqa: E402

import os
from pathlib import Path

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pg = pytest.importorskip("pyqtgraph")

from PySide6.QtWidgets import QApplication

from probe_station_gui.design.model import DesignDocument, MeasurementTarget, SnapResult
from probe_station_gui.design.plot_interaction import GuidePreview
from probe_station_gui.design.plot_presentation import PlotPresentation
from probe_station_gui.route.model import MeasurementRoute, RoutePoint
from probe_station_gui.views.design_plot_rendering import DesignPlotRenderer
from probe_station_gui.views.design_plot_viewport import DesignPlotViewport


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _document(path: Path) -> DesignDocument:
    return DesignDocument(
        path=path,
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


def test_renderer_never_owns_or_hides_preexisting_raster(
    qt_app: QApplication,
) -> None:
    plot = pg.PlotWidget()
    raster = pg.ImageItem()
    plot.addItem(raster)
    renderer = DesignPlotRenderer(plot, DesignPlotViewport(plot), parent=plot)
    created_items = set(plot.plotItem.items) - {raster}

    renderer.set_preview_visibility(False)

    assert raster.isVisible()
    assert created_items
    assert all(not item.isVisible() for item in created_items)
    renderer.shutdown()
    plot.deleteLater()


def test_renderer_applies_immutable_plan_and_fixed_pixel_overlays(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    plot = pg.PlotWidget()
    viewport = DesignPlotViewport(plot)
    renderer = DesignPlotRenderer(plot, viewport, parent=plot)
    presentation = PlotPresentation()
    document = _document(tmp_path / "chip.gds")
    presentation.set_document(document)
    presentation.set_targets(
        [MeasurementTarget("target", "Target", (10.0, 20.0))],
        selected_target_id="target",
    )
    presentation.set_current_design_position((5.0, 6.0), fov_design_size=(4.0, 2.0))

    renderer.render(presentation.refresh())

    assert renderer.state.target_points == ((10.0, 20.0),)
    assert renderer.state.selected_target_points == ((10.0, 20.0),)
    assert renderer.state.current_crosshair_coordinate_count == 5
    renderer.shutdown()
    plot.deleteLater()


def test_hover_segment_and_preview_visibility_are_renderer_owned(
    qt_app: QApplication,
) -> None:
    plot = pg.PlotWidget()
    renderer = DesignPlotRenderer(plot, DesignPlotViewport(plot), parent=plot)
    result = SnapResult(
        point=(5.0, 0.0),
        mode="segment_center",
        distance=0.1,
        segment_start=(0.0, 0.0),
        segment_end=(10.0, 0.0),
    )

    renderer.set_hover(result)
    renderer.set_preview_visibility(False)

    assert renderer.state.hover_segment == ((0.0, 0.0), (10.0, 0.0))
    assert renderer.state.overlays_visible is False
    renderer.shutdown()
    plot.deleteLater()


def test_renderer_applies_normalized_presentation_mutation(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    plot = pg.PlotWidget()
    viewport = DesignPlotViewport(plot)
    presentation = PlotPresentation()
    renderer = DesignPlotRenderer(plot, viewport, parent=plot)
    document = _document(tmp_path / "session.gds")
    presentation.set_document(document)
    viewport.set_content(document.bounds, None, None, recompute=True)

    renderer.apply(
        presentation.set_targets,
        [MeasurementTarget("target", "Target", (3.0, 4.0))],
        selected_target_id="target",
    )

    assert presentation.plan.targets[0].design_center == (3.0, 4.0)
    assert renderer.state.selected_target_points == ((3.0, 4.0),)
    renderer.shutdown()
    plot.deleteLater()


def test_renderer_consumes_visual_interaction_effects(
    qt_app: QApplication,
) -> None:
    plot = pg.PlotWidget()
    presentation = PlotPresentation()
    renderer = DesignPlotRenderer(plot, DesignPlotViewport(plot), parent=plot)

    handled = renderer.apply_interaction_effect(
        GuidePreview(((1.0, 2.0), (3.0, 4.0))), presentation
    )

    assert handled is True
    assert presentation.plan.tool_sketch_points == ((1.0, 2.0), (3.0, 4.0))
    renderer.shutdown()
    plot.deleteLater()


def test_renderer_applies_navigation_before_route_render(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    plot = pg.PlotWidget()
    viewport = DesignPlotViewport(plot)
    presentation = PlotPresentation()
    document = _document(tmp_path / "route.gds")
    document.path.write_bytes(b"gds")
    route = MeasurementRoute.default_for_document(document)
    route.points.append(RoutePoint("far", "Far", (1_000.0, 0.0)))
    presentation.set_document(document)
    viewport.apply_presentation(presentation, recompute=True)
    renderer = DesignPlotRenderer(plot, viewport, parent=plot)

    renderer.apply_navigation(
        presentation,
        presentation.set_probe_route,
        route,
        selected_route_point_index=-1,
    )

    assert viewport.content_bounds is not None
    assert viewport.content_bounds[2] == 1_000.0
    renderer.shutdown()
    plot.deleteLater()
