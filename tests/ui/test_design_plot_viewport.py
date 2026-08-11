from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from PySide6.QtCore import QEvent, Qt

from probe_station_gui.design.markup import MarkupDocument
from probe_station_gui.design.model import DesignDocument, SnapResult
from probe_station_gui.design.plot_interaction import (
    HoverEffect,
    MouseButton,
    PlotAction,
    PlotInteraction,
    SnapClickIntent,
)
from probe_station_gui.design.plot_presentation import PlotPresentation
from probe_station_gui.design.selection_geometry import PointGeometry
from probe_station_gui.design.selection_model import EntityOwner, SelectableDesignEntity
from probe_station_gui.route.model import MeasurementRoute, RoutePoint
from probe_station_gui.design.snap_coordinator import HoverPublication
from probe_station_gui.views import design_plot_viewport as viewport_module
from probe_station_gui.views.design_plot_viewport import DesignPlotViewport


class _Rect:
    def __init__(self, width: float, height: float) -> None:
        self._width = width
        self._height = height

    def width(self) -> float:
        return self._width

    def height(self) -> float:
        return self._height

    def contains(self, _position) -> bool:
        return True


class _Point:
    def __init__(self, x: float, y: float) -> None:
        self._x = x
        self._y = y

    def x(self) -> float:
        return self._x

    def y(self) -> float:
        return self._y


class _ViewBox:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.range = [[0.0, 100.0], [0.0, 50.0]]
        self.limits = {}
        self.rect = _Rect(500.0, 250.0)

    def sceneBoundingRect(self):
        return self.rect

    def viewRange(self):
        return self.range

    def setLimits(self, **limits) -> None:
        self.events.append("limits")
        self.limits = limits

    def setRange(self, *, xRange, yRange, padding: float) -> None:
        del padding
        self.events.append("range")
        self.range = [list(xRange), list(yRange)]

    def viewPixelSize(self):
        return (0.2, -0.25)

    def mapViewToScene(self, point):
        return _Point(point.x() * 2.0, point.y() * 2.0)

    def mapSceneToView(self, point):
        return _Point(point.x() / 2.0, point.y() / 2.0)


class _Plot:
    def __init__(self) -> None:
        self.view_box = _ViewBox()

    def getViewBox(self):
        return self.view_box

    def sceneBoundingRect(self):
        return self.view_box.rect


def _document(path: Path) -> DesignDocument:
    path.write_bytes(b"gds")
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


def test_navigation_limits_are_applied_before_initial_focus(tmp_path: Path) -> None:
    plot = _Plot()
    viewport = DesignPlotViewport(plot)
    document = _document(tmp_path / "chip.gds")

    viewport.set_content(document.bounds, None, None, recompute=True, focus=True)

    assert plot.view_box.events[:2] == ["limits", "range"]
    assert viewport.frame is not None


def test_resize_refits_cached_content_without_rescanning_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plot = _Plot()
    viewport = DesignPlotViewport(plot)
    document = _document(tmp_path / "chip.gds")
    route = MeasurementRoute.default_for_document(document)
    route.points.append(RoutePoint("far", "Far", (10_000.0, 0.0)))
    markup = MarkupDocument.empty(document.path)
    calls = 0
    real_content_bounds = viewport_module.content_bounds

    def counted_content_bounds(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_content_bounds(*args, **kwargs)

    monkeypatch.setattr(viewport_module, "content_bounds", counted_content_bounds)
    viewport.set_content(document.bounds, route, markup, recompute=True)
    calls_before_resize = calls
    plot.view_box.rect = _Rect(900.0, 300.0)

    viewport.resize()

    assert calls == calls_before_resize
    assert viewport.viewport_size() == (900.0, 300.0)


def test_view_capture_restore_pixel_metrics_and_scene_normalization() -> None:
    plot = _Plot()
    viewport = DesignPlotViewport(plot)
    captured = viewport.capture_view_range()

    viewport.restore_view_range(((10.0, 20.0), (30.0, 40.0)))
    normalized = viewport.scene_to_design(_Point(8.0, 10.0))

    assert captured == ((0.0, 100.0), (0.0, 50.0))
    assert plot.view_box.range == [[10.0, 20.0], [30.0, 40.0]]
    assert normalized == (4.0, 5.0)
    assert viewport.data_units_per_screen_pixel() == pytest.approx(0.25)
    assert viewport.screen_distance((0.0, 0.0), (3.0, 4.0)) == pytest.approx(10.0)


def test_clear_removes_all_navigation_limits() -> None:
    plot = _Plot()
    viewport = DesignPlotViewport(plot)

    viewport.clear()

    assert viewport.frame is None
    assert plot.view_box.limits == {
        "xMin": None,
        "xMax": None,
        "yMin": None,
        "yMax": None,
        "maxXRange": None,
        "maxYRange": None,
    }


def test_click_event_is_normalized_at_the_viewport_seam() -> None:
    viewport = DesignPlotViewport(_Plot())
    event = SimpleNamespace(
        scenePos=lambda: _Point(8.0, 10.0),
        button=lambda: Qt.LeftButton,
        modifiers=lambda: Qt.ShiftModifier,
        double=lambda: True,
    )

    click = viewport.plot_click(event)

    assert click.button is MouseButton.LEFT
    assert click.point == (4.0, 5.0)
    assert click.modifiers.shift is True
    assert click.double is True


def test_click_transition_checks_presentation_context_at_viewport_seam(
    tmp_path: Path,
) -> None:
    viewport = DesignPlotViewport(_Plot())
    interaction = PlotInteraction()
    interaction.set_context(document_present=True, preview_active=False)
    interaction.set_tool("move")
    interaction.set_navigation_enabled(True)
    presentation = PlotPresentation()
    presentation.set_document(_document(tmp_path / "click.gds"))
    event = SimpleNamespace(
        scenePos=lambda: _Point(8.0, 10.0),
        button=lambda: Qt.LeftButton,
        modifiers=lambda: Qt.NoModifier,
        double=lambda: False,
    )

    transition = viewport.click_transition(event, interaction, presentation)

    assert isinstance(transition.effects[0], SnapClickIntent)
    assert transition.effects[0].action is PlotAction.MOVE


def test_selection_press_dispatch_owns_qt_event_normalization() -> None:
    viewport = DesignPlotViewport(_Plot())
    interaction = PlotInteraction()
    interaction.set_context(document_present=True, preview_active=False)
    interaction.set_tool("select")
    presentation = PlotPresentation()
    entity = SelectableDesignEntity(
        id="route:p1",
        owner=EntityOwner.ROUTE,
        source_id="p1",
        geometry=PointGeometry((4.0, 5.0)),
    )
    presentation.set_selectable_entities((entity,))
    event = SimpleNamespace(
        type=lambda: QEvent.GraphicsSceneMousePress,
        scenePos=lambda: _Point(8.0, 10.0),
        button=lambda: Qt.LeftButton,
        modifiers=lambda: Qt.NoModifier,
    )

    result = viewport.dispatch_scene_event(
        event,
        interaction=interaction,
        presentation=presentation,
        drag_threshold=10,
        tolerance=1.0,
    )

    assert result.handled is True
    assert result.transition is not None
    assert interaction.selection_active is True


def test_presentation_navigation_and_hover_are_normalized_at_viewport_seam(
    tmp_path: Path,
) -> None:
    viewport = DesignPlotViewport(_Plot())
    presentation = PlotPresentation()
    document = _document(tmp_path / "presentation.gds")
    presentation.set_document(document)

    viewport.apply_presentation(presentation, recompute=True, focus=True)
    hover = viewport.hover_input(_Point(8.0, 10.0), Qt.ControlModifier)

    assert viewport.content_bounds == document.bounds
    assert hover.inside is True
    assert hover.point == (4.0, 5.0)
    assert hover.modifiers.control is True


def test_hover_publication_is_normalized_at_viewport_input_seam() -> None:
    viewport = DesignPlotViewport(_Plot())
    interaction = PlotInteraction()
    interaction.set_context(document_present=True, preview_active=False)
    result = SnapResult((4.0, 5.0), "free", 0.0)

    transition = viewport.complete_hover_publication(
        interaction,
        HoverPublication(
            result=result,
            raw_point=(4.0, 5.0),
            shift=True,
            control=False,
            generation=None,
            elapsed_ms=None,
        ),
    )

    assert transition.effects == (HoverEffect(result, True, False),)
