from __future__ import annotations

from dataclasses import replace
import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QAction, QWheelEvent
from PySide6.QtWidgets import QAbstractButton, QApplication

from probe_station_gui.design.markup import GuideSegment
from probe_station_gui.route.model import MeasurementRoute, NeedleOffset, RoutePoint
from probe_station_gui.views import design_plot_viewport as viewport_module
from tests.ui.design_plot_klayout_support import (
    _assert_gds_focus,
    _box,
    _view_box,
)


pytest_plugins = ("tests.ui.design_plot_klayout_fixtures",)


def test_open_frames_gds_but_internal_limits_include_distant_hidden_markup(
    pane, document, distant_hidden_markup
) -> None:
    pane.set_document(document)
    pane.set_markup(distant_hidden_markup)

    _assert_gds_focus(pane, document)
    assert pane._viewport.frame[2] > 1_000_000.0


def test_route_and_markup_updates_expand_limits_without_changing_view(
    pane, document, distant_route, distant_hidden_markup
) -> None:
    pane.set_document(document)
    before = _box(pane)

    pane.set_probe_route(distant_route, selected_route_point_index=-1)
    pane.set_markup(distant_hidden_markup)

    assert _box(pane) == pytest.approx(before)
    assert pane._viewport.frame[2] > 1_000_000.0


def test_deleting_outer_content_shrinks_and_clamps_once(
    pane, document, distant_route
) -> None:
    pane.set_document(document)
    pane.set_probe_route(distant_route, selected_route_point_index=-1)
    _view_box(pane).setRange(
        xRange=(999_900.0, 1_000_100.0),
        yRange=(-100.0, 100.0),
        padding=0.0,
    )

    pane.set_probe_route(None, selected_route_point_index=-1)

    assert pane._viewport.frame[2] < 1_000.0
    assert _box(pane)[2] <= pane._viewport.frame[2]


def test_shrink_preserves_an_already_valid_view(pane, document, distant_route) -> None:
    pane.set_document(document)
    pane.set_probe_route(distant_route, selected_route_point_index=-1)
    _view_box(pane).setRange(
        xRange=(10.0, 40.0),
        yRange=(5.0, 20.0),
        padding=0.0,
    )
    before = _box(pane)

    pane.set_probe_route(None, selected_route_point_index=-1)

    assert _box(pane) == pytest.approx(before)


def test_home_restores_gds_without_changing_content_limits(
    window, document, distant_route
) -> None:
    window.set_document(document)
    window.set_probe_route(distant_route, selected_route_point_index=-1)
    frame = window._main_view._viewport.frame
    window._home_shortcut.activated.emit()

    assert window._main_view._viewport.frame == frame
    _assert_gds_focus(window._main_view, document)


def test_needle_offset_update_expands_then_shrinks_navigation_frame(
    pane, document
) -> None:
    route = MeasurementRoute.default_for_document(document)
    route.points.append(RoutePoint("local", "Local", (20.0, 20.0)))
    pane.set_document(document)
    pane.set_probe_route(route, selected_route_point_index=-1)
    original = pane._viewport.frame

    route.needle_offsets = [NeedleOffset("N1", "Needle 1", 2_000_000.0, 0.0)]
    pane.set_probe_route(route, selected_route_point_index=-1)
    assert pane._viewport.frame[2] > 2_000_000.0

    route.needle_offsets = [NeedleOffset("N1", "Needle 1", 0.0, 0.0)]
    pane.set_probe_route(route, selected_route_point_index=-1)
    assert pane._viewport.frame == pytest.approx(original)


def test_rotated_document_ignores_stale_content_until_models_are_refreshed(
    pane, document, distant_route, distant_hidden_markup
) -> None:
    pane.set_document(document)
    pane.set_probe_route(distant_route, selected_route_point_index=-1)
    pane.set_markup(distant_hidden_markup)
    assert pane._viewport.frame[2] > 1_000_000.0

    rotated = replace(
        document,
        bounds=(0.0, 0.0, 50.0, 100.0),
        rotation_quarter_turns=1,
        source_load_id="rotated-load",
    )
    pane.set_document(rotated)
    assert pane._viewport.frame[2] < 1_000.0

    rotated_route = MeasurementRoute.default_for_document(rotated)
    rotated_route.points.append(RoutePoint("far", "Far", (0.0, 1_000_000.0)))
    rotated_markup = replace(
        distant_hidden_markup,
        guides=(
            GuideSegment(
                "rotated-guide",
                (0.0, 1_000_000.0),
                (-100.0, 1_000_100.0),
            ),
        ),
    )
    pane.set_probe_route(rotated_route, selected_route_point_index=-1)
    pane.set_markup(rotated_markup)
    assert pane._viewport.frame[3] > 1_000_000.0


def test_design_window_exposes_no_fit_all_control_or_action(window) -> None:
    button_texts = {
        button.text().replace("&", "")
        for button in window.findChildren(QAbstractButton)
    }
    action_texts = {
        action.text().replace("&", "") for action in window.findChildren(QAction)
    }
    assert all("fit all" not in text.casefold() for text in button_texts)
    assert all("fit all" not in text.casefold() for text in action_texts)


def test_file_backed_renderer_receives_gds_focused_range_on_first_request(
    pane, document
) -> None:
    pane.set_document(document)

    first_range = pane._raster_controller.ranges_seen_on_set_document[0]
    assert first_range == pytest.approx(_box(pane))
    assert abs(first_range[2] - first_range[0]) < 2.0 * (
        document.bounds[2] - document.bounds[0]
    )


@pytest.mark.parametrize(
    ("dx", "dy"),
    [
        (-1_000_000.0, 0.0),
        (1_000_000.0, 0.0),
        (0.0, -1_000_000.0),
        (0.0, 1_000_000.0),
    ],
)
def test_viewbox_cannot_pan_past_any_navigation_edge(
    pane, document, qt_app, dx, dy
) -> None:
    pane.set_document(document)
    frame = pane._viewport.frame
    view_box = _view_box(pane)
    view_box.setRange(
        xRange=(20.0, 80.0),
        yRange=(10.0, 40.0),
        padding=0.0,
    )
    view_box.translateBy(x=dx, y=dy)
    qt_app.processEvents()

    visible = _box(pane)
    assert visible[0] >= frame[0] - 1e-6
    assert visible[1] >= frame[1] - 1e-6
    assert visible[2] <= frame[2] + 1e-6
    assert visible[3] <= frame[3] + 1e-6


def test_wheel_zoom_out_stops_at_navigation_frame(pane, document, qt_app) -> None:
    pane.set_document(document)
    frame = pane._viewport.frame
    before = _box(pane)
    viewport = pane._plot.viewport()
    center = viewport.rect().center()
    wheel = QWheelEvent(
        QPointF(center),
        QPointF(viewport.mapToGlobal(center)),
        QPoint(),
        QPoint(0, -12_000),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.ScrollUpdate,
        False,
    )

    QApplication.sendEvent(viewport, wheel)
    qt_app.processEvents()

    visible = _box(pane)
    assert visible[2] - visible[0] > before[2] - before[0]
    assert visible[0] >= frame[0] - 1e-6
    assert visible[1] >= frame[1] - 1e-6
    assert visible[2] <= frame[2] + 1e-6
    assert visible[3] <= frame[3] + 1e-6


def test_resize_recomputes_aspect_frame_without_refocusing(
    pane, document, distant_route, qt_app
) -> None:
    pane.resize(800, 600)
    pane.show()
    qt_app.processEvents()
    pane.set_document(document)
    pane.set_probe_route(distant_route, selected_route_point_index=-1)
    _view_box(pane).setRange(
        xRange=(1_000.0, 1_100.0),
        yRange=(-25.0, 25.0),
        padding=0.0,
    )
    before = _box(pane)
    before_center = ((before[0] + before[2]) * 0.5, (before[1] + before[3]) * 0.5)

    pane.resize(900, 300)
    qt_app.processEvents()

    frame = pane._viewport.frame
    after = _box(pane)
    after_center = ((after[0] + after[2]) * 0.5, (after[1] + after[3]) * 0.5)
    viewport_width, viewport_height = pane._viewport.viewport_size()
    frame_aspect = (frame[2] - frame[0]) / (frame[3] - frame[1])
    assert frame_aspect == pytest.approx(
        viewport_width / viewport_height,
        rel=0.05,
    )
    assert after_center == pytest.approx(before_center)


def test_resize_refits_cached_content_without_rescanning_models(
    pane, document, distant_route, qt_app, monkeypatch
) -> None:
    real_content_bounds = viewport_module.content_bounds
    calls = 0

    def counted_content_bounds(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_content_bounds(*args, **kwargs)

    monkeypatch.setattr(viewport_module, "content_bounds", counted_content_bounds)
    pane.set_document(document)
    pane.set_probe_route(distant_route, selected_route_point_index=-1)
    scans_before_resize = calls

    pane.resize(900, 300)
    qt_app.processEvents()

    assert calls == scans_before_resize
